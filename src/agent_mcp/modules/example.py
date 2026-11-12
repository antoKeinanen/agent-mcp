from datetime import UTC, datetime

from pydantic import BaseModel

from agent_mcp.module import Module, tool


class ExampleSettings(BaseModel):
    greeting: str = "hello"


class Example(Module[ExampleSettings]):
    """Reference module. Copy this file to start a new one."""

    name = "example"
    Settings = ExampleSettings

    @tool(read_only=True)
    def greet(self, who: str) -> str:
        """Greet someone using the configured greeting."""
        return f"{self.settings.greeting}, {who}"

    @tool(read_only=True)
    async def now(self) -> str:
        """Current UTC time in ISO 8601."""
        return datetime.now(UTC).isoformat()
