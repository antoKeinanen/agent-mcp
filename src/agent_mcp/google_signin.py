"""Google sign-in that needs no terminal, browser callback or open port, shared by Google modules.

1. Without a refresh token the server still starts and hands out a sign-in URL.
2. After signing in, Google sends the browser to http://localhost/?code=... . That page
   doesn't load, which is fine: its URL holds the code.
3. That URL goes to the module's `sign_in` tool (exchanged at once, no restart) or into
   AGENT_MCP__<MODULE>__AUTH_CODE (exchanged on the next start). The refresh token is saved
   to `token_file` and reused from then on. A code works once.
"""

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from agent_mcp.config import ENV_PREFIX
from agent_mcp.module import ToolError

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REDIRECT_URI = "http://localhost"

type TokenProvider = Callable[[], Awaitable[str]]


def parse_code(value: str) -> str:
    """The code from a pasted redirect URL, or the value itself if it's a bare code."""
    value = value.strip().strip('"').strip("'")
    if "code=" in value:
        query = urlsplit(value).query or value.split("?", 1)[-1]
        if codes := parse_qs(query).get("code"):
            return codes[0]
    return value


def _fingerprint(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()[:16]


def load_token(path: Path, client_id: str) -> tuple[str | None, str | None]:
    """(refresh token, fingerprint of the code it came from), if saved for this client."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, None
    if data.get("client_id") != client_id:
        return None, None
    return data.get("refresh_token"), data.get("code")


def save_token(path: Path, client_id: str, refresh_token: str, code: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"client_id": client_id, "refresh_token": refresh_token, "code": _fingerprint(code)}, f)
    tmp.replace(path)


@dataclass(frozen=True)
class GoogleSignIn:
    """Sign-in for one module: its name (for env vars and the tool name), product and scopes."""

    module: str
    product: str
    scopes: tuple[str, ...]
    error: type[ToolError] = ToolError

    def var(self, field: str) -> str:
        return f"{ENV_PREFIX}{self.module.upper()}__{field}"

    def url(self, client_id: str) -> str:
        params = {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{AUTH_URI}?{urlencode(params)}"

    def help(self, client_id: str, reason: str | None = None) -> str:
        reason = reason or f"{self.product} is not signed in."
        return (
            f"{reason} To sign in: (1) open {self.url(client_id)} and allow access; "
            "(2) the browser then goes to a localhost page that doesn't load, which is expected: copy the full "
            f"URL from the address bar; (3) give that URL to the agent, which passes it to {self.module}_sign_in "
            f'right away. Or put it in .env as {self.var("AUTH_CODE")}="<the URL>" and restart '
            "(docker compose up -d) within 10 minutes. Each code works once."
        )

    async def exchange_code(self, http: httpx.AsyncClient, client_id: str, client_secret: str, code: str) -> str:
        r = await http.post(
            TOKEN_URI,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.is_error:
            error = body.get("error", f"HTTP {r.status_code}")
            detail = f": {body['error_description']}" if body.get("error_description") else ""
            if error == "invalid_grant":
                reason = f"Google says the sign-in code has expired or was already used (invalid_grant{detail})."
            else:
                reason = f"Google rejected the sign-in code ({error}{detail})."
            raise self.error(self.help(client_id, reason))
        if not body.get("refresh_token"):
            raise self.error(self.help(client_id, "Google returned no refresh token."))
        return body["refresh_token"]

    async def complete(
        self, http: httpx.AsyncClient, client_id: str, client_secret: str, code: str, token_file: Path | None
    ) -> tuple[str, str | None]:
        """Exchange a code and try to save the token. Returns (token, warning if it couldn't be saved).

        A failed save never loses the token: the code is spent by now, so the caller keeps the
        token in memory rather than failing and retrying a code Google will refuse."""
        token = await self.exchange_code(http, client_id, client_secret, code)
        if token_file is None:
            return token, f"Signed in, but {self.var('TOKEN_FILE')} isn't set, so the sign-in is lost on restart."
        try:
            save_token(token_file, client_id, token, code)
        except OSError as e:
            return token, f"Signed in, but saving the sign-in to {token_file} failed ({e}); it is lost on restart."
        return token, None

    async def refresh_token(
        self,
        http: httpx.AsyncClient,
        client_id: str,
        client_secret: str,
        configured: str | None,
        token_file: Path | None,
        auth_code: str | None,
    ) -> tuple[str, str | None]:
        """Pick the refresh token: configured > saved (unless a new code was pasted) > exchanged code.

        Returns (token, warning)."""
        if configured:
            return configured, None
        code = parse_code(auth_code) if auth_code else None
        saved, saved_code = load_token(token_file, client_id) if token_file else (None, None)
        if saved and (code is None or saved_code == _fingerprint(code)):
            return saved, None
        if code is None:
            raise self.error(self.help(client_id))
        return await self.complete(http, client_id, client_secret, code, token_file)

    def token_provider(
        self, client_id: str, client_secret: str, refresh_token: str, refused: str | None = None
    ) -> TokenProvider:
        """Access tokens minted from a refresh token, renewed shortly before they expire.

        `refused` is the message the agent gets if Google rejects the refresh token."""
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials(
            None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri=TOKEN_URI,
            scopes=list(self.scopes),
        )
        lock = asyncio.Lock()
        message = refused or self.help(client_id, "Google refused the saved sign-in (revoked or expired).")

        async def token() -> str:
            async with lock:
                if not creds.valid:
                    try:
                        await asyncio.to_thread(creds.refresh, Request())
                    except RefreshError as e:
                        raise self.error(message) from e
                return creds.token

        return token
