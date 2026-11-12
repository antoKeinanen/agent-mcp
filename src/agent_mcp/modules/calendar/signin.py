"""Google sign-in for the calendar module. The flow lives in `agent_mcp.google_signin`."""

from pathlib import Path

import httpx

from agent_mcp.google_signin import REDIRECT_URI, GoogleSignIn, load_token, parse_code, save_token

from .google import SCOPES, CalendarError

__all__ = [
    "AUTH_CODE_VAR",
    "REDIRECT_URI",
    "SIGN_IN",
    "complete_sign_in",
    "exchange_code",
    "load_token",
    "parse_code",
    "refresh_token",
    "save_token",
    "sign_in_help",
    "sign_in_url",
]

SIGN_IN = GoogleSignIn("calendar", "Google Calendar", tuple(SCOPES), CalendarError)
AUTH_CODE_VAR = SIGN_IN.var("AUTH_CODE")


def sign_in_url(client_id: str) -> str:
    return SIGN_IN.url(client_id)


def sign_in_help(client_id: str, reason: str | None = None) -> str:
    return SIGN_IN.help(client_id, reason)


async def exchange_code(http: httpx.AsyncClient, client_id: str, client_secret: str, code: str) -> str:
    return await SIGN_IN.exchange_code(http, client_id, client_secret, code)


async def complete_sign_in(
    http: httpx.AsyncClient, client_id: str, client_secret: str, code: str, token_file: Path | None
) -> tuple[str, str | None]:
    return await SIGN_IN.complete(http, client_id, client_secret, code, token_file)


async def refresh_token(
    http: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
    configured: str | None,
    token_file: Path | None,
    auth_code: str | None,
) -> tuple[str, str | None]:
    return await SIGN_IN.refresh_token(http, client_id, client_secret, configured, token_file, auth_code)
