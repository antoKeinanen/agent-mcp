"""Google Tasks, limited to one task list.

The agent can list, add, edit, complete and delete todos, but only in the list named by
`task_list`. It can't see that other lists exist. Every request names that list, and each
existing todo is checked to be in it before it is changed. The OAuth scope covers all of
your Google Tasks, so this module's code is what keeps the agent in its list.
"""

import logging
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from agent_mcp.google_signin import GoogleSignIn, TokenProvider, parse_code
from agent_mcp.module import Module, ToolError, tool

log = logging.getLogger("agent_mcp.todo")

API = "https://tasks.googleapis.com/tasks/v1"
SCOPES = ("https://www.googleapis.com/auth/tasks",)


class TodoError(ToolError):
    """A failure whose message the agent should see."""


SIGN_IN = GoogleSignIn("todo", "Google Tasks", SCOPES, TodoError)


class TodoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_list: str = Field(description="The one Google Tasks list the agent may use. Name or ID.")
    client_id: str | None = Field(None, description="Google OAuth client ID (Desktop app). Can be the calendar's.")
    client_secret: SecretStr | None = None
    refresh_token: SecretStr | None = Field(None, description="Written by `agent-mcp-google-auth todo`, or leave unset.")
    auth_code: SecretStr | None = Field(None, description="Redirect URL (or code) from the sign-in link. Used once.")
    token_file: Path | None = Field(None, description="Where a refresh token from sign-in is kept.")


class GoogleTasks:
    """Minimal async Google Tasks v1 client. Every task call is scoped to a list ID."""

    def __init__(self, token: TokenProvider, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token = token
        self._http = httpx.AsyncClient(base_url=API, transport=transport, timeout=30)

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        r = await self._http.request(method, path, headers=headers, **kw)
        if r.status_code == 404:
            raise TodoError("not found in the todo list")
        if r.is_error:
            raise TodoError(f"{method} {path}: {r.status_code} {r.text[:500]}")
        return r.json() if r.content else {}

    async def lists(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {"maxResults": 100}
        while True:
            page = await self._request("GET", "/users/@me/lists", params=params)
            items += page.get("items", [])
            if not (token := page.get("nextPageToken")):
                return items
            params["pageToken"] = token

    async def tasks(self, list_id: str, show_completed: bool, due_max: str | None, limit: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "maxResults": 100,
            "showCompleted": str(show_completed).lower(),
            "showHidden": str(show_completed).lower(),
        }
        if due_max:
            params["dueMax"] = due_max
        while len(items) < limit:
            page = await self._request("GET", f"/lists/{_q(list_id)}/tasks", params=params)
            items += page.get("items", [])
            if not (token := page.get("nextPageToken")):
                break
            params["pageToken"] = token
        return items[:limit]

    async def task(self, list_id: str, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/lists/{_q(list_id)}/tasks/{_q(task_id)}")

    async def insert(self, list_id: str, body: dict[str, Any], parent: str | None) -> dict[str, Any]:
        params = {"parent": parent} if parent else {}
        return await self._request("POST", f"/lists/{_q(list_id)}/tasks", params=params, json=body)

    async def patch(self, list_id: str, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PATCH", f"/lists/{_q(list_id)}/tasks/{_q(task_id)}", json=body)

    async def delete(self, list_id: str, task_id: str) -> None:
        await self._request("DELETE", f"/lists/{_q(list_id)}/tasks/{_q(task_id)}")


class Todo(Module[TodoSettings]):
    name = "todo"
    Settings = TodoSettings

    api: GoogleTasks | None = None
    http: httpx.AsyncClient | None = None  # the sign-in code exchange
    signed_out: str | None = None  # why `api` is missing, shown to the agent
    _list_id: str | None = None

    async def start(self) -> None:
        """Never fails for missing sign-in: the server stays up and the tools explain what to do."""
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=30, headers={"User-Agent": "agent-mcp"})
        if self.api is not None:
            return
        s = self.settings
        if not (s.client_id and s.client_secret):
            self.signed_out = (
                f"Google Tasks is not configured: set {SIGN_IN.var('CLIENT_ID')} and {SIGN_IN.var('CLIENT_SECRET')} "
                "(Google Cloud > Credentials > OAuth client ID > Desktop app)."
            )
        else:
            try:
                token, warning = await SIGN_IN.refresh_token(
                    self.http,
                    s.client_id,
                    s.client_secret.get_secret_value(),
                    s.refresh_token.get_secret_value() if s.refresh_token else None,
                    s.token_file.expanduser() if s.token_file else None,
                    s.auth_code.get_secret_value() if s.auth_code else None,
                )
            except TodoError as e:
                self.signed_out = str(e)
            except httpx.HTTPError as e:
                self.signed_out = SIGN_IN.help(s.client_id, f"Could not reach Google to finish sign-in ({e!r}).")
            except Exception as e:  # never take the server down over sign-in
                log.exception("todo: sign-in failed")
                self.signed_out = SIGN_IN.help(s.client_id, f"Sign-in failed unexpectedly ({e!r}).")
            else:
                self._signed_in(token, warning)
                return
        log.warning("todo: %s", self.signed_out)

    def _signed_in(self, token: str, warning: str | None) -> None:
        s = self.settings
        assert s.client_id and s.client_secret
        self.api = GoogleTasks(SIGN_IN.token_provider(s.client_id, s.client_secret.get_secret_value(), token))
        self.signed_out = None
        if warning:
            log.warning("todo: %s", warning)
        else:
            log.info("todo: signed in to Google")

    async def stop(self) -> None:
        if self.api is not None:
            await self.api.close()
        if self.http is not None:
            await self.http.aclose()

    # ---- sign-in -------------------------------------------------------------------------

    @tool
    async def sign_in(self, redirect_url: str) -> dict[str, Any]:
        """Finish Google sign-in with the URL the user's browser landed on after allowing access
        (http://localhost/?code=..., a page that doesn't load). Call it as soon as the user gives
        you that URL: the code in it expires within minutes and works once. Only works while
        signed out; the error from any other todo tool contains the sign-in link to give them.
        """
        s = self.settings
        if self.api is not None:
            raise TodoError("Already signed in to Google. Nothing to do.")
        if not (s.client_id and s.client_secret):
            raise TodoError(self.signed_out or f"Set {SIGN_IN.var('CLIENT_ID')} and CLIENT_SECRET first.")
        code = parse_code(redirect_url)
        if not code or code.startswith("http"):
            raise TodoError(SIGN_IN.help(s.client_id, "That URL has no ?code=... in it."))
        assert self.http is not None, "module not started"
        try:
            token, warning = await SIGN_IN.complete(
                self.http,
                s.client_id,
                s.client_secret.get_secret_value(),
                code,
                s.token_file.expanduser() if s.token_file else None,
            )
        except httpx.HTTPError as e:
            raise TodoError(f"Could not reach Google ({e!r}). Try again with the same URL.") from e
        self._signed_in(token, warning)
        return {"signed_in": True, "saved": warning is None, **({"warning": warning} if warning else {})}

    # ---- todos ---------------------------------------------------------------------------

    @tool(read_only=True)
    async def items(self, include_completed: bool = False, due_by: str | None = None, limit: int = 100) -> dict[str, Any]:
        """List the todos in your todo list. Open ones only unless `include_completed`.
        `due_by` (YYYY-MM-DD) keeps only todos due on or before that day."""
        if not 1 <= limit <= 1000:
            raise TodoError("limit must be between 1 and 1000")
        due_max = _due(_date(due_by)) if due_by else None
        list_id = await self._list()
        found = await self._api.tasks(list_id, include_completed, due_max, limit)
        return {"list": self.settings.task_list, "items": [_summary(t) for t in found]}

    @tool
    async def add(self, title: str, notes: str = "", due: str | None = None, parent_id: str | None = None) -> dict[str, Any]:
        """Add a todo to your todo list. `due` is a date (YYYY-MM-DD); Google Tasks keeps no time
        of day. `parent_id` makes it a subtask of an existing todo in the list."""
        if not title.strip():
            raise TodoError("title must not be empty")
        list_id = await self._list()
        if parent_id:
            await self._task(list_id, parent_id)
        body: dict[str, Any] = {"title": title, "notes": notes}
        if due:
            body["due"] = _due(_date(due))
        return _summary(await self._api.insert(list_id, body, parent_id))

    @tool
    async def update(
        self, task_id: str, title: str | None = None, notes: str | None = None, due: str | None = None
    ) -> dict[str, Any]:
        """Change a todo's title, notes or due date (YYYY-MM-DD). Leave a field out to keep it;
        pass `due=""` to remove the due date."""
        body: dict[str, Any] = {}
        if title is not None:
            if not title.strip():
                raise TodoError("title must not be empty")
            body["title"] = title
        if notes is not None:
            body["notes"] = notes
        if due is not None:
            body["due"] = _due(_date(due)) if due else None
        if not body:
            raise TodoError("nothing to change: give title, notes or due")
        list_id = await self._list()
        await self._task(list_id, task_id)
        return _summary(await self._api.patch(list_id, task_id, body))

    @tool
    async def complete(self, task_id: str, done: bool = True) -> dict[str, Any]:
        """Mark a todo done, or not done again with `done=false`."""
        list_id = await self._list()
        await self._task(list_id, task_id)
        body = {"status": "completed"} if done else {"status": "needsAction", "completed": None}
        return _summary(await self._api.patch(list_id, task_id, body))

    @tool(destructive=True)
    async def delete(self, task_id: str) -> dict[str, Any]:
        """Delete a todo for good. Prefer `complete` for finished ones."""
        list_id = await self._list()
        task = await self._task(list_id, task_id)
        await self._api.delete(list_id, task_id)
        return {"deleted": True, "title": task.get("title")}

    # ---- helpers -------------------------------------------------------------------------

    @property
    def _api(self) -> GoogleTasks:
        if self.api is None:
            raise TodoError(self.signed_out or "todo module not started")
        return self.api

    async def _list(self) -> str:
        """The configured list's ID: by ID, else exact name, else case-insensitive name."""
        if self._list_id is None:
            ref = self.settings.task_list
            lists = await self._api.lists()
            for match in (
                lambda t: t["id"] == ref,
                lambda t: t.get("title") == ref,
                lambda t: (t.get("title") or "").casefold() == ref.casefold(),
            ):
                found = [t for t in lists if match(t)]
                if len(found) > 1:
                    raise TodoError(f"more than one Google Tasks list is named {ref!r}; set task_list to its ID")
                if found:
                    self._list_id = found[0]["id"]
                    break
            else:
                raise TodoError(f"no Google Tasks list named {ref!r}; create it in Google Tasks first")
        return self._list_id

    async def _task(self, list_id: str, task_id: str) -> dict[str, Any]:
        """A todo, checked to belong to the configured list."""
        task = await self._api.task(list_id, task_id)
        if f"/lists/{list_id}/tasks/" not in task.get("selfLink", ""):
            raise TodoError("not found in the todo list")
        return task


def _date(s: str) -> date:
    try:
        return date.fromisoformat(s.strip())
    except ValueError as e:
        raise TodoError(f"not a date (YYYY-MM-DD): {s!r}") from e


def _due(d: date) -> str:
    return f"{d.isoformat()}T00:00:00.000Z"


def _summary(t: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in {
            "id": t.get("id"),
            "title": t.get("title"),
            "notes": t.get("notes") or None,
            "due": t["due"][:10] if t.get("due") else None,
            "done": t.get("status") == "completed",
            "completed": t.get("completed"),
            "parent_id": t.get("parent"),
            "updated": t.get("updated"),
            "link": t.get("webViewLink"),
        }.items()
        if v is not None
    }


def _q(s: str) -> str:
    return quote(s, safe="")
