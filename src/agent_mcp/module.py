from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

__all__ = ["BoundTool", "Module", "NoSettings", "ToolError", "ToolInfo", "tool"]


class NoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ToolInfo:
    description: str | None
    read_only: bool
    destructive: bool


_TOOL_ATTR = "__agent_mcp_tool__"


def tool[F: Callable[..., Any]](
    fn: F | None = None,
    *,
    description: str | None = None,
    read_only: bool = False,
    destructive: bool = False,
) -> Any:
    """Mark a Module method as a tool. The docstring is the description unless one is given."""

    def mark(f: F) -> F:
        setattr(f, _TOOL_ATTR, ToolInfo(description, read_only, destructive))
        return f

    return mark(fn) if fn is not None else mark


@dataclass(frozen=True)
class BoundTool:
    name: str
    fn: Callable[..., Any]
    info: ToolInfo


class Module[S: BaseModel = NoSettings]:
    """Base class for a group of tools that share settings and lifecycle.

    Subclasses set `name`, optionally `Settings`, and decorate methods with `@tool`.
    """

    name: ClassVar[str]
    description: ClassVar[str] = ""
    Settings: ClassVar[type[BaseModel]] = NoSettings

    def __init__(self, settings: S) -> None:
        self.settings = settings

    async def start(self) -> None:
        """Acquire resources (clients, connections) before the server accepts calls."""

    async def stop(self) -> None:
        """Release what `start` acquired."""

    @classmethod
    def tool_names(cls) -> list[str]:
        return [n for n in dir(cls) if hasattr(getattr(cls, n), _TOOL_ATTR)]

    def bound_tools(self) -> dict[str, BoundTool]:
        return {
            n: BoundTool(n, getattr(self, n), getattr(getattr(type(self), n), _TOOL_ATTR))
            for n in self.tool_names()
        }
