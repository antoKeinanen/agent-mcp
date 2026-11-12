import importlib
import inspect
import pkgutil
from types import ModuleType

from agent_mcp.module import Module

type Registry = dict[str, type[Module]]


def discover(package: ModuleType) -> Registry:
    """Find every concrete Module subclass defined in the submodules of `package`."""
    found: Registry = {}
    for info in pkgutil.iter_modules(package.__path__, f"{package.__name__}."):
        for _, obj in inspect.getmembers(importlib.import_module(info.name), inspect.isclass):
            if issubclass(obj, Module) and obj is not Module and obj.__module__ == info.name:
                if obj.name in found:
                    raise ValueError(f"duplicate module name {obj.name!r}")
                found[obj.name] = obj
    return found


def builtin() -> Registry:
    import agent_mcp.modules

    return discover(agent_mcp.modules)
