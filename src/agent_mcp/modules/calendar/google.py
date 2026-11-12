"""Minimal async Google Calendar v3 client.

It can read anything, but the only write it has is `insert_event`. The module decides
where that may be called. There is deliberately no update, delete, patch or RSVP method.
"""

from typing import Any

import httpx

from agent_mcp.google_signin import AUTH_URI, TOKEN_URI, TokenProvider
from agent_mcp.module import ToolError

__all__ = ["AUTH_URI", "SCOPES", "TOKEN_URI", "CalendarError", "GoogleCalendar", "oauth_token_provider"]

API = "https://www.googleapis.com/calendar/v3"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


class CalendarError(ToolError):
    """A failure whose message the agent should see."""


def oauth_token_provider(
    client_id: str, client_secret: str, refresh_token: str, refused: str = "Google refused the refresh token."
) -> TokenProvider:
    """Access tokens minted from a refresh token, renewed shortly before they expire.

    `refused` is the message the agent gets if Google rejects the refresh token."""
    from agent_mcp.google_signin import GoogleSignIn

    return GoogleSignIn("calendar", "Google Calendar", tuple(SCOPES), CalendarError).token_provider(
        client_id, client_secret, refresh_token, refused
    )


class GoogleCalendar:
    def __init__(self, token: TokenProvider, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token = token
        self._http = httpx.AsyncClient(base_url=API, transport=transport, timeout=30)

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        r = await self._http.request(method, path, headers=headers, **kw)
        if r.is_error:
            raise CalendarError(f"{method} {path}: {r.status_code} {r.text[:500]}")
        return r.json()

    async def _paged(self, path: str, params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        params = {k: v for k, v in params.items() if v is not None}
        while len(items) < limit:
            page = await self._request("GET", path, params=params)
            items += page.get("items", [])
            if not (token := page.get("nextPageToken")):
                break
            params["pageToken"] = token
        return items[:limit]

    async def calendars(self) -> list[dict[str, Any]]:
        return await self._paged("/users/me/calendarList", {"showHidden": "true", "maxResults": 250}, 10_000)

    async def events(
        self,
        calendar_id: str,
        time_min: str,
        time_max: str,
        query: str | None = None,
        limit: int = 250,
        private_property: str | None = None,
    ) -> list[dict[str, Any]]:
        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "q": query,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": min(limit, 2500),
            "privateExtendedProperty": private_property,
        }
        return await self._paged(f"/calendars/{_q(calendar_id)}/events", params, limit)

    async def event(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}")

    async def insert_event(self, calendar_id: str, body: dict[str, Any], send_updates: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/calendars/{_q(calendar_id)}/events", params={"sendUpdates": send_updates}, json=body
        )


def _q(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")
