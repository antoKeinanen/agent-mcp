from pathlib import Path

import pytest

from agent_mcp import registry
from agent_mcp.config import ConfigError, load, plan
from agent_mcp.modules.calendar import Calendar


def test_env_alone_configures_server_and_modules() -> None:
    env = {
        "AGENT_MCP__SERVER__PORT": "9000",
        "AGENT_MCP__CALENDAR__TOOLS": "events, schedule",
        "AGENT_MCP__CALENDAR__CLIENT_SECRET": "s3cret",
        "AGENT_MCP__CALENDAR__SCHEDULING_CALENDAR": "ai scheduling",
        "agent_mcp__calendar__organizations__TKO__calendar": "TKO-äly",
        "AGENT_MCP__CALENDAR__ORGANIZATIONS__TKO__IGNORE": "gurula smash,board game weekly",
        "AGENT_MCP__CALENDAR__ORGANIZATIONS__TKO__DEFAULT_DURATION_MINUTES": "90",
        "PATH": "/usr/bin",
    }
    cfg = load(None, env, registry.builtin())
    assert cfg.server.port == 9000
    [exposed] = plan(cfg, registry.builtin())
    assert [t.name for t in exposed.tools] == ["events", "schedule"]
    s = exposed.module.settings
    assert isinstance(exposed.module, Calendar)
    assert s.client_secret.get_secret_value() == "s3cret"
    org = s.organizations["tko"]
    assert org.ignore == ["gurula smash", "board game weekly"]
    assert org.default_duration_minutes == 90


def test_env_overrides_toml(tmp_path: Path) -> None:
    toml = tmp_path / "c.toml"
    toml.write_text('[modules.example]\ntools = ["greet"]\n[modules.example.settings]\ngreeting = "hei"\n')
    env = {"AGENT_MCP__EXAMPLE__GREETING": "moi", "AGENT_MCP__EXAMPLE__TOOLS": "*"}
    [exposed] = plan(load(toml, env, registry.builtin()), registry.builtin())
    assert exposed.module.settings.greeting == "moi"
    assert {t.name for t in exposed.tools} == {"greet", "now"}


def test_json_lists_work_too() -> None:
    env = {"AGENT_MCP__EXAMPLE__TOOLS": '["greet"]'}
    [exposed] = plan(load(None, env, registry.builtin()), registry.builtin())
    assert [t.name for t in exposed.tools] == ["greet"]


@pytest.mark.parametrize(
    ("env", "fragment"),
    [
        ({"AGENT_MCP__CALENDAR": "x"}, "expected agent_mcp__"),
        ({"AGENT_MCP__CALENDAR__TIMEZONE": "Europe/Helsinki"}, "tools"),  # settings alone expose nothing
        ({"AGENT_MCP__EXAMPLE__TOOLS": "*", "AGENT_MCP__EXAMPLE__TYPO": "1"}, "no such setting"),
    ],
)
def test_bad_env_is_rejected(env: dict, fragment: str) -> None:
    with pytest.raises(ConfigError) as err:
        plan(load(None, env, registry.builtin()), registry.builtin())
    assert fragment in str(err.value).lower()
