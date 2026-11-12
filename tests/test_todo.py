import json
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from mcp import Client

from agent_mcp import registry
from agent_mcp.config import Config, ConfigError, load, plan
from agent_mcp.modules.todo import GoogleTasks, Todo
from agent_mcp.server import build

AGENT = "agentListId"
PRIVATE = "privateListId"
SELF = "https://www.googleapis.com/tasks/v1/lists/{}/tasks/{}"


class FakeTasks:
    """In-memory Tasks API with two lists. Records every request path."""

    def __init__(self) -> None:
        self.lists = [{"id": AGENT, "title": "Agent"}, {"id": PRIVATE, "title": "Private"}]
        self.tasks: dict[str, list[dict[str, Any]]] = {AGENT: [], PRIVATE: []}
        self.paths: list[str] = []
        self.add(PRIVATE, "secret", "p1")

    def add(self, list_id: str, title: str, task_id: str, **extra: Any) -> dict[str, Any]:
        t = {"id": task_id, "title": title, "status": "needsAction", "selfLink": SELF.format(list_id, task_id), **extra}
        self.tasks[list_id].append(t)
        return t

    def _find(self, task_id: str) -> tuple[str, dict[str, Any]] | None:
        # Like the real API, a task ID is found even through another list's path.
        return next(((lid, t) for lid, ts in self.tasks.items() for t in ts if t["id"] == task_id), None)

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = unquote(req.url.path.removeprefix("/tasks/v1"))
        self.paths.append(f"{req.method} {path}")
        if path == "/users/@me/lists":
            return httpx.Response(200, json={"items": self.lists})
        parts = path.split("/")  # ['', 'lists', list_id, 'tasks', (task_id)]
        list_id = parts[2]
        if len(parts) == 4 and req.method == "GET":
            items = self.tasks[list_id]
            if req.url.params.get("showCompleted") == "false":
                items = [t for t in items if t["status"] != "completed"]
            if due_max := req.url.params.get("dueMax"):
                items = [t for t in items if t.get("due") and t["due"] <= due_max]
            return httpx.Response(200, json={"items": items})
        if len(parts) == 4 and req.method == "POST":
            body = json.loads(req.content)
            n = sum(len(v) for v in self.tasks.values())
            extra = {"parent": req.url.params["parent"]} if "parent" in req.url.params else {}
            return httpx.Response(200, json=self.add(list_id, body.pop("title"), f"t{n}", **body, **extra))
        found = self._find(parts[4])
        if found is None:
            return httpx.Response(404, json={"error": {"message": "Not Found"}})
        owner, task = found
        if req.method == "GET":
            return httpx.Response(200, json=task)
        if req.method == "PATCH":
            for k, v in json.loads(req.content).items():
                if v is None:
                    task.pop(k, None)
                else:
                    task[k] = v
            if task["status"] == "completed":
                task.setdefault("completed", "2026-11-12T10:00:00.000Z")
            return httpx.Response(200, json=task)
        if req.method == "DELETE":
            self.tasks[owner].remove(task)
            return httpx.Response(204)
        return httpx.Response(405)


async def fake_token() -> str:
    return "access"


@pytest.fixture
def fake() -> FakeTasks:
    return FakeTasks()


def client(fake: FakeTasks, task_list: str = "Agent") -> Client:
    cfg = Config.model_validate({"modules": {"todo": {"tools": ["*"], "settings": {"task_list": task_list}}}})
    [e] = plan(cfg, {"todo": Todo})
    assert isinstance(e.module, Todo)
    e.module.api = GoogleTasks(fake_token, httpx.MockTransport(fake.handle))
    return Client(build(cfg, [e]))


async def call(c: Client, name: str, **args: Any) -> Any:
    r = await c.call_tool(f"todo_{name}", args)
    assert not r.is_error, r.content[0].text
    return r.structured_content.get("result", r.structured_content)


async def error(c: Client, name: str, **args: Any) -> str:
    r = await c.call_tool(f"todo_{name}", args)
    assert r.is_error
    return r.content[0].text


async def test_add_list_complete_update_delete(fake: FakeTasks) -> None:
    async with client(fake) as c:
        added = await call(c, "add", title="Buy milk", notes="oat", due="2026-11-20")
        assert added == {"id": "t1", "title": "Buy milk", "notes": "oat", "due": "2026-11-20", "done": False}
        assert fake.tasks[AGENT][0]["due"] == "2026-11-20T00:00:00.000Z"

        sub = await call(c, "add", title="Check price", parent_id="t1")
        assert sub["parent_id"] == "t1"

        listed = await call(c, "items")
        assert listed["list"] == "Agent" and [t["title"] for t in listed["items"]] == ["Buy milk", "Check price"]
        assert [t["title"] for t in (await call(c, "items", due_by="2026-11-30"))["items"]] == ["Buy milk"]

        done = await call(c, "complete", task_id="t2")
        assert done["done"] and "completed" in done
        assert [t["id"] for t in (await call(c, "items"))["items"]] == ["t1"]
        assert len((await call(c, "items", include_completed=True))["items"]) == 2
        reopened = await call(c, "complete", task_id="t2", done=False)
        assert not reopened["done"] and "completed" not in reopened

        updated = await call(c, "update", task_id="t1", title="Buy oat milk", due="")
        assert updated["title"] == "Buy oat milk" and "due" not in updated

        assert await call(c, "delete", task_id="t2") == {"deleted": True, "title": "Check price"}
        assert [t["id"] for t in fake.tasks[AGENT]] == ["t1"]


async def test_other_lists_are_never_touched(fake: FakeTasks) -> None:
    async with client(fake) as c:
        assert "lists" not in {t.name.removeprefix("todo_") for t in (await c.list_tools()).tools}
        assert [t["title"] for t in (await call(c, "items", include_completed=True))["items"]] == []
        for name, args in [
            ("update", {"task_id": "p1", "title": "pwned"}),
            ("complete", {"task_id": "p1"}),
            ("delete", {"task_id": "p1"}),
            ("add", {"title": "sub", "parent_id": "p1"}),
        ]:
            assert "not found in the todo list" in await error(c, name, **args)
    assert fake.tasks[PRIVATE] == [
        {"id": "p1", "title": "secret", "status": "needsAction", "selfLink": SELF.format(PRIVATE, "p1")}
    ]
    assert not [p for p in fake.paths if PRIVATE in p and not p.startswith("GET")]
    assert all(AGENT in p or p == "GET /users/@me/lists" for p in fake.paths)


async def test_list_is_found_by_id_or_any_case_name(fake: FakeTasks) -> None:
    for ref in (AGENT, "agent"):
        async with client(fake, ref) as c:
            await call(c, "add", title="x")
    assert len(fake.tasks[AGENT]) == 2


async def test_missing_or_ambiguous_list_is_an_error(fake: FakeTasks) -> None:
    async with client(fake, "Nope") as c:
        assert "no Google Tasks list named 'Nope'" in await error(c, "items")
    fake.lists.append({"id": "dup", "title": "agent"})
    async with client(fake, "AGENT") as c:
        assert "more than one" in await error(c, "items")


async def test_bad_arguments(fake: FakeTasks) -> None:
    async with client(fake) as c:
        assert "not a date" in await error(c, "add", title="x", due="next friday")
        assert "empty" in await error(c, "add", title="  ")
        assert "nothing to change" in await error(c, "update", task_id="t1")
    assert not [p for p in fake.paths if not p.startswith("GET")]


async def test_signed_out_tools_carry_a_todo_sign_in_link() -> None:
    cfg = Config.model_validate(
        {"modules": {"todo": {"tools": ["*"], "settings": {"task_list": "Agent", "client_id": "cid", "client_secret": "s"}}}}
    )
    async with Client(build(cfg, plan(cfg, {"todo": Todo}))) as c:
        text = await error(c, "items")
    assert "accounts.google.com" in text and "auth%2Ftasks" in text
    assert "todo_sign_in" in text and "AGENT_MCP__TODO__AUTH_CODE" in text


def test_task_list_is_required_and_env_works() -> None:
    with pytest.raises(ConfigError) as err:
        plan(load(None, {"AGENT_MCP__TODO__TOOLS": "items"}, registry.builtin()), registry.builtin())
    assert "task_list" in str(err.value)
    env = {"AGENT_MCP__TODO__TOOLS": "items,add", "AGENT_MCP__TODO__TASK_LIST": "Agent"}
    [e] = plan(load(None, env, registry.builtin()), registry.builtin())
    assert isinstance(e.module, Todo) and e.module.settings.task_list == "Agent"
