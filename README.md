<img src="assets/icon.svg" width="64" height="64" alt="">

# agent-mcp

An MCP server that gives an agent (Hermes) access to a chosen subset of your tools. Tools live in modules. A config file decides which modules load and which of their tools are exposed. Nothing is exposed unless the config names it.

## Run

```sh
uv sync
cp agent-mcp.example.toml agent-mcp.toml
uv run agent-mcp --list     # print the tools this config exposes
uv run agent-mcp            # serve over stdio
```

Point Hermes at it in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  agent-mcp:
    command: uv
    args: ["run", "--directory", "/path/to/agent-mcp", "agent-mcp", "-c", "/path/to/agent-mcp.toml"]
```

## Config

```toml
[server]
transport = "stdio"           # or "streamable-http" (binds host/port, default 127.0.0.1:8765)
audit_log = "audit.jsonl"     # optional; one JSON line per tool call

[modules.example]
tools = ["greet"]             # required; ["*"] exposes every tool in the module
# enabled = false             # keep the section but load nothing

[modules.example.settings]    # validated against the module's Settings model
greeting = "hei"
```

Startup fails, listing every problem, on an unknown module, an unknown tool name, or invalid settings. Exposed tools are named `<module>_<tool>`.

## Writing a module

Add a file under `src/agent_mcp/modules/`. Any `Module` subclass there is discovered by its `name`.

```python
from pydantic import BaseModel
from agent_mcp.module import Module, tool

class NotesSettings(BaseModel):
    root: str

class Notes(Module[NotesSettings]):
    name = "notes"
    Settings = NotesSettings

    async def start(self) -> None: ...   # open clients before calls arrive
    async def stop(self) -> None: ...    # close them on shutdown

    @tool(read_only=True)
    def search(self, query: str) -> list[str]:
        """Search notes for a phrase."""   # becomes the tool description
        ...

    @tool(destructive=True)
    async def delete(self, note_id: str) -> None:
        """Delete a note."""
        ...
```

Parameters and return types become the tool's JSON schema. `read_only` and `destructive` are sent to the agent as MCP tool hints. They don't enforce anything. The allowlist is what restricts access.

## Test

```sh
uv run pytest
```
