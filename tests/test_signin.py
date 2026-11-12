import json
import stat
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from mcp import Client

from agent_mcp.config import Config, plan
from agent_mcp.modules.calendar import Calendar
from agent_mcp.modules.calendar.signin import parse_code, sign_in_url
from agent_mcp.server import build
from test_calendar import SETTINGS

REDIRECT = "http://localhost/?code=4/0Abc-DEF&scope=https://www.googleapis.com/auth/calendar.readonly"


class FakeTokenEndpoint:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status, self.body = status, body or {"refresh_token": "1//refresh", "access_token": "a"}
        self.calls: list[dict[str, list[str]]] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        assert str(req.url) == "https://oauth2.googleapis.com/token"
        self.calls.append(parse_qs(req.content.decode()))
        return httpx.Response(self.status, json=self.body)


def module(settings: dict, endpoint: FakeTokenEndpoint | None = None) -> Calendar:
    cfg = Config.model_validate({"modules": {"calendar": {"tools": ["*"], "settings": {**SETTINGS, **settings}}}})
    [e] = plan(cfg, {"calendar": Calendar})
    m = e.module
    assert isinstance(m, Calendar)
    m.http = httpx.AsyncClient(transport=httpx.MockTransport((endpoint or FakeTokenEndpoint()).handle))
    return m


CLIENT = {"client_id": "cid.apps.googleusercontent.com", "client_secret": "csecret"}


def test_parse_code_accepts_url_or_bare_code() -> None:
    assert parse_code(REDIRECT) == "4/0Abc-DEF"
    assert parse_code(f'"{REDIRECT}"') == "4/0Abc-DEF"
    assert parse_code("4/0Abc-DEF") == "4/0Abc-DEF"


def test_sign_in_url_asks_for_offline_calendar_access() -> None:
    q = parse_qs(sign_in_url("cid").split("?", 1)[1])
    assert q["client_id"] == ["cid"] and q["redirect_uri"] == ["http://localhost"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert "https://www.googleapis.com/auth/calendar.events" in q["scope"][0].split()


async def test_server_starts_without_credentials_and_tools_explain() -> None:
    cfg = Config.model_validate({"modules": {"calendar": {"tools": ["*"], "settings": SETTINGS}}})
    async with Client(build(cfg, plan(cfg, {"calendar": Calendar}))) as c:
        r = await c.call_tool("calendar_calendars", {})
        assert r.is_error and "CLIENT_ID" in r.content[0].text
        assert not (await c.call_tool("calendar_organizations", {})).is_error


async def test_without_token_the_error_carries_a_sign_in_link() -> None:
    m = module(CLIENT)
    await m.start()
    assert m.api is None
    assert "accounts.google.com" in m.signed_out and "client_id=cid.apps" in m.signed_out
    assert "AGENT_MCP__CALENDAR__AUTH_CODE" in m.signed_out


async def test_auth_code_is_exchanged_once_and_saved(tmp_path: Path) -> None:
    token_file = tmp_path / "data" / "google-token.json"
    endpoint = FakeTokenEndpoint()
    m = module({**CLIENT, "auth_code": REDIRECT, "token_file": str(token_file)}, endpoint)
    await m.start()
    assert m.api is not None and m.signed_out is None
    [call] = endpoint.calls
    assert call["code"] == ["4/0Abc-DEF"] and call["grant_type"] == ["authorization_code"]
    assert call["redirect_uri"] == ["http://localhost"]
    saved = json.loads(token_file.read_text())
    assert saved["refresh_token"] == "1//refresh" and "4/0Abc" not in token_file.read_text()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    again = module({**CLIENT, "auth_code": REDIRECT, "token_file": str(token_file)}, endpoint)
    await again.start()  # restart with the same, now-used code: reuse the saved token
    assert again.api is not None and len(endpoint.calls) == 1


async def test_new_auth_code_replaces_saved_token(tmp_path: Path) -> None:
    token_file = tmp_path / "t.json"
    endpoint = FakeTokenEndpoint()
    await module({**CLIENT, "auth_code": "first", "token_file": str(token_file)}, endpoint).start()
    endpoint.body = {"refresh_token": "1//second"}
    await module({**CLIENT, "auth_code": "second", "token_file": str(token_file)}, endpoint).start()
    assert len(endpoint.calls) == 2
    assert json.loads(token_file.read_text())["refresh_token"] == "1//second"


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (400, {"error": "invalid_grant"}, "expired or was already used"),
        (401, {"error": "invalid_client"}, "rejected AGENT_MCP__CALENDAR__AUTH_CODE (invalid_client)"),
        (200, {"access_token": "a"}, "no refresh token"),
    ],
)
async def test_failed_exchange_keeps_server_up_with_fresh_link(
    tmp_path: Path, status: int, body: dict, message: str
) -> None:
    m = module({**CLIENT, "auth_code": "c", "token_file": str(tmp_path / "t.json")}, FakeTokenEndpoint(status, body))
    await m.start()
    assert m.api is None and message in m.signed_out and "accounts.google.com" in m.signed_out
    assert not (tmp_path / "t.json").exists()


async def test_auth_code_without_token_file_is_explained() -> None:
    m = module({**CLIENT, "auth_code": "c"})
    await m.start()
    assert m.api is None and "TOKEN_FILE" in m.signed_out


async def test_configured_refresh_token_wins(tmp_path: Path) -> None:
    endpoint = FakeTokenEndpoint()
    m = module({**CLIENT, "refresh_token": "1//env", "auth_code": "c", "token_file": str(tmp_path / "t")}, endpoint)
    await m.start()
    assert m.api is not None and endpoint.calls == []
