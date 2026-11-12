import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_mcp.module import BoundTool, Module
from agent_mcp.registry import Registry

ALL_TOOLS = "*"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModuleConfig(_Strict):
    enabled: bool = True
    tools: list[str] = Field(
        description='Tools to expose. Required, so nothing is exposed by accident. ["*"] exposes all.'
    )
    settings: dict[str, Any] = {}


class ServerConfig(_Strict):
    name: str = "agent-mcp"
    instructions: str | None = None
    transport: Literal["stdio", "streamable-http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8765
    audit_log: Path | None = None


class Config(_Strict):
    server: ServerConfig = ServerConfig()
    modules: dict[str, ModuleConfig] = {}


class ConfigError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("invalid config:\n" + "\n".join(f"  - {p}" for p in problems))
        self.problems = problems


def load(path: Path) -> Config:
    try:
        return Config.model_validate(tomllib.loads(path.read_text()))
    except (tomllib.TOMLDecodeError, ValidationError) as e:
        raise ConfigError([f"{path}: {e}"]) from e


@dataclass(frozen=True)
class Exposed:
    module: Module
    tools: list[BoundTool]


def plan(config: Config, registry: Registry) -> list[Exposed]:
    """Instantiate enabled modules and pick their allowlisted tools. Raises ConfigError listing every problem."""
    problems: list[str] = []
    exposed: list[Exposed] = []
    for name, mc in config.modules.items():
        cls = registry.get(name)
        if cls is None:
            problems.append(f"modules.{name}: unknown module (known: {', '.join(sorted(registry)) or 'none'})")
            continue
        if not mc.enabled:
            continue
        try:
            settings = cls.Settings.model_validate(mc.settings)
        except ValidationError as e:
            problems.append(f"modules.{name}.settings: {e}")
            continue
        available = cls.tool_names()
        wanted = available if mc.tools == [ALL_TOOLS] else mc.tools
        unknown = [t for t in wanted if t not in available]
        if unknown:
            problems.append(f"modules.{name}.tools: unknown {unknown} (available: {available})")
            continue
        module = cls(settings)
        bound = module.bound_tools()
        exposed.append(Exposed(module, [bound[t] for t in wanted]))
    if problems:
        raise ConfigError(problems)
    return exposed
