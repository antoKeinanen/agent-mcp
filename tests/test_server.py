import pytest
from mcp import Client
from pydantic import BaseModel

from agent_mcp.config import Config, ConfigError, plan
from agent_mcp.module import Module, tool
from agent_mcp.server import build


class CounterSettings(BaseModel):
    step: int = 1


events: list[str] = []


class Counter(Module[CounterSettings]):
    name = "counter"
    Settings = CounterSettings

    async def start(self) -> None:
        self.value = 0
        events.append("start")

    async def stop(self) -> None:
        events.append("stop")

    @tool
    def bump(self) -> int:
        """Increment and return the counter."""
        self.value += self.settings.step
        return self.value

    @tool(destructive=True)
    def reset(self) -> int:
        self.value = 0
        return 0


REGISTRY = {"counter": Counter}


def server_for(modules: dict) -> object:
    cfg = Config.model_validate({"modules": modules})
    return build(cfg, plan(cfg, REGISTRY))


async def test_only_allowlisted_tools_are_listed_and_callable() -> None:
    async with Client(server_for({"counter": {"tools": ["bump"], "settings": {"step": 5}}})) as c:
        assert [t.name for t in (await c.list_tools()).tools] == ["counter_bump"]
        first = await c.call_tool("counter_bump", {})
        second = await c.call_tool("counter_bump", {})
        assert first.structured_content == {"result": 5}
        assert second.structured_content == {"result": 10}
        assert (await c.call_tool("counter_reset", {})).is_error


async def test_star_exposes_all_tools_with_annotations() -> None:
    async with Client(server_for({"counter": {"tools": ["*"]}})) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert set(tools) == {"counter_bump", "counter_reset"}
        assert tools["counter_bump"].description == "Increment and return the counter."
        assert tools["counter_reset"].annotations.destructive_hint is True


async def test_unconfigured_and_disabled_modules_expose_nothing() -> None:
    for modules in ({}, {"counter": {"enabled": False, "tools": ["*"]}}):
        async with Client(server_for(modules)) as c:
            assert (await c.list_tools()).tools == []


async def test_lifecycle_runs_around_the_session() -> None:
    events.clear()
    async with Client(server_for({"counter": {"tools": ["bump"]}})) as c:
        await c.call_tool("counter_bump", {})
        assert events == ["start"]
    assert events == ["start", "stop"]


@pytest.mark.parametrize(
    ("modules", "fragment"),
    [
        ({"nope": {"tools": ["*"]}}, "modules.nope: unknown module"),
        ({"counter": {"tools": ["bump", "drop_db"]}}, "unknown ['drop_db']"),
        ({"counter": {"tools": ["*"], "settings": {"step": "x"}}}, "modules.counter.settings"),
        ({"counter": {}}, "tools"),
    ],
)
def test_bad_config_is_rejected_at_startup(modules: dict, fragment: str) -> None:
    with pytest.raises((ConfigError, ValueError)) as err:
        server_for(modules)
    assert fragment in str(err.value)


def test_allowed_hosts_lock_host_header_and_refuse_browsers() -> None:
    from agent_mcp.config import ServerConfig
    from agent_mcp.server import transport_security

    assert transport_security(ServerConfig()) is None
    ts = transport_security(ServerConfig(allowed_hosts=["agent-mcp", "mcp.local:9000"]))
    assert ts.enable_dns_rebinding_protection
    assert ts.allowed_hosts == ["agent-mcp", "agent-mcp:*", "mcp.local:9000"]
    assert ts.allowed_origins == []
