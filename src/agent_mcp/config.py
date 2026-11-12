import json
import tomllib
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_mcp.module import BoundTool, Module
from agent_mcp.registry import Registry

ALL_TOOLS = "*"
ENV_PREFIX = "AGENT_MCP__"
ENV_SEP = "__"


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
    allowed_hosts: list[str] = Field(
        [],
        description="streamable-http only: Host headers to accept, e.g. the container's DNS name. "
        "A name without a port also matches any port. Empty keeps the SDK default "
        "(localhost names when bound to localhost, anything otherwise).",
    )
    audit_log: Path | None = None


class Config(_Strict):
    server: ServerConfig = ServerConfig()
    modules: dict[str, ModuleConfig] = {}


class ConfigError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("invalid config:\n" + "\n".join(f"  - {p}" for p in problems))
        self.problems = problems


def load(path: Path | None, env: Mapping[str, str] | None = None, registry: Registry | None = None) -> Config:
    """Read the TOML file (optional) and overlay `AGENT_MCP__*` variables from `env`.

    Variables mirror the TOML layout with `__` between levels and win over the file:
      AGENT_MCP__SERVER__TRANSPORT=stdio                 -> [server] transport
      AGENT_MCP__CALENDAR__TOOLS=events,schedule         -> [modules.calendar] tools
      AGENT_MCP__CALENDAR__TIMEZONE=Europe/Helsinki      -> [modules.calendar.settings] timezone
      AGENT_MCP__CALENDAR__ORGANIZATIONS__TKO__IGNORE=a,b -> [...settings.organizations.tko] ignore
    Names are case-insensitive and keys come out lowercase. List fields take comma-separated
    values or JSON.
    """
    data: dict[str, Any] = {}
    source = "environment"
    try:
        if path is not None:
            source = str(path)
            data = tomllib.loads(path.read_text())
        if env:
            source = f"{source} + environment" if path else source
            data = _merge(data, _from_env(env, registry or {}))
        return Config.model_validate(data)
    except (tomllib.TOMLDecodeError, ValidationError, OSError) as e:
        raise ConfigError([f"{source}: {e}"]) from e


MODULE_KEYS = ("tools", "enabled")


def _from_env(env: Mapping[str, str], registry: Registry) -> dict[str, Any]:
    out: dict[str, Any] = {}
    problems: list[str] = []
    for key, raw in env.items():
        if not key.upper().startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in key[len(ENV_PREFIX) :].split(ENV_SEP)]
        if len(parts) < 2 or not all(parts):
            problems.append(f"{key}: expected {ENV_PREFIX}<SERVER|MODULE>__<FIELD>[__...]")
            continue
        head, rest = parts[0], parts[1:]
        model: type[BaseModel] | None
        if head == "server":
            path, model = ["server", *rest], ServerConfig
        elif len(rest) == 1 and rest[0] in MODULE_KEYS:
            path, model = ["modules", head, *rest], ModuleConfig
        else:
            model = registry[head].Settings if head in registry else None
            path = ["modules", head, "settings", *rest]
        tp = _annotation(model, rest)
        if tp is _UNKNOWN:
            problems.append(f"{key}: no such setting")
            continue
        cursor = out
        for p in path[:-1]:
            cursor = cursor.setdefault(p, {})
        cursor[path[-1]] = _coerce(tp, raw)
    if problems:
        raise ConfigError(problems)
    return out


_UNKNOWN = object()


def _annotation(model: type[BaseModel] | None, fields: list[str]) -> Any:
    """The type at `fields` inside `model`, stepping through models and dict values.

    `_UNKNOWN` when a model has no such field; None when the type can't be followed further
    (e.g. an unknown module, left for `plan` to report)."""
    tp: Any = model
    for name in fields:
        tp = _unwrap_optional(tp)
        if isinstance(tp, type) and issubclass(tp, BaseModel):
            field = tp.model_fields.get(name)
            if field is None:
                return _UNKNOWN
            tp = field.annotation
        elif typing.get_origin(tp) is dict:
            tp = typing.get_args(tp)[1]
        else:
            return None
    return _unwrap_optional(tp)


def _unwrap_optional(tp: Any) -> Any:
    if typing.get_origin(tp) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp


def _coerce(tp: Any, raw: str) -> Any:
    value = raw.strip()
    if value[:1] in "[{":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    if typing.get_origin(tp) in (list, set, tuple):
        return [v.strip() for v in value.split(",") if v.strip()]
    return raw


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


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
