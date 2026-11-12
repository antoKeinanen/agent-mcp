import functools
import inspect
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from agent_mcp.config import Config, Exposed

audit = logging.getLogger("agent_mcp.audit")


def exposed_name(module: str, tool: str) -> str:
    return f"{module}_{tool}"


def _audited(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    def record(start: float, kwargs: dict[str, Any], error: BaseException | None) -> None:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "tool": name,
            "ok": error is None,
            "ms": round((time.perf_counter() - start) * 1000, 1),
            "args": {k: v for k, v in kwargs.items() if not k.startswith("_")},
        }
        if error is not None:
            entry["error"] = repr(error)
        audit.info(json.dumps(entry, default=repr))

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def run_async(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
            except BaseException as e:
                record(start, kwargs, e)
                raise
            record(start, kwargs, None)
            return result

        return run_async

    @functools.wraps(fn)
    def run_sync(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except BaseException as e:
            record(start, kwargs, e)
            raise
        record(start, kwargs, None)
        return result

    return run_sync


def build(config: Config, exposed: list[Exposed]) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for e in exposed:
                await e.module.start()
                stack.push_async_callback(e.module.stop)
            yield

    server: MCPServer = MCPServer(
        config.server.name,
        instructions=config.server.instructions,
        lifespan=lifespan,
    )
    for e in exposed:
        for t in e.tools:
            name = exposed_name(e.module.name, t.name)
            server.add_tool(
                _audited(name, t.fn),
                name=name,
                description=t.info.description or inspect.getdoc(t.fn),
                annotations=ToolAnnotations(
                    read_only_hint=t.info.read_only,
                    destructive_hint=t.info.destructive,
                ),
            )
    return server
