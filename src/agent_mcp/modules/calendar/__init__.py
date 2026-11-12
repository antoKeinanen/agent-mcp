"""Google Calendar: read everything, write only by inviting an organization's calendar.

The trick: events are created in a hidden "scheduling" calendar and invite a visible
organization calendar as an attendee. They then show up in the visible calendar as
pending invitations (RSVP needsAction), so you can pick which ones to go to.

The agent can read every calendar. Its only write is `schedule`, which:
  * creates an event in the organization's scheduling calendar, never anywhere else,
  * invites exactly one attendee, the organization's calendar, as needsAction,
  * refuses titles on the organization's ignore list,
  * skips events already present in either calendar.
"""

import time
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from agent_mcp.module import Module, tool

from .google import CalendarError, GoogleCalendar, oauth_token_provider
from .matching import matches_pattern, similar

ORG_KEY = "agent_mcp_org"
SOURCE_KEY = "agent_mcp_source"
CALENDAR_CACHE_SECONDS = 300
WRITABLE = {"owner", "writer"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Organization(_Strict):
    name: str | None = Field(None, description="Display name. Defaults to the config key.")
    calendar: str = Field(description="Calendar to invite (shown to you). Name or calendar ID.")
    scheduling_calendar: str | None = Field(
        None, description="Hidden calendar events are created in. Defaults to the module-wide one."
    )
    sources: list[str] = Field([], description="Where the organization lists its events, for the agent.")
    instructions: str = Field("", description="Extra guidance handed to the agent with this organization.")
    ignore: list[str] = Field([], description="Titles never to schedule. Fuzzy-matched.")
    estimated_end_prefix: str = "[#]"
    estimated_end_note: str = "Note: the end time is estimated because the source did not clearly state one."
    default_duration_minutes: int = Field(120, gt=0, description="Used when no end time is given.")
    send_updates: Literal["none", "all", "externalOnly"] = "none"


class CalendarSettings(_Strict):
    client_id: str | None = Field(None, description="Google OAuth client ID (Desktop app).")
    client_secret: SecretStr | None = None
    refresh_token: SecretStr | None = Field(None, description="Written by `agent-mcp-google-auth`.")
    timezone: str = "Europe/Helsinki"
    scheduling_calendar: str | None = Field(None, description="Default hidden calendar for every organization.")
    organizations: dict[str, Organization] = {}

    @model_validator(mode="after")
    def _check(self) -> "CalendarSettings":
        ZoneInfo(self.timezone)
        for key, org in self.organizations.items():
            host = org.scheduling_calendar or self.scheduling_calendar
            if host is None:
                raise ValueError(f"organizations.{key}: no scheduling_calendar (set it here or module-wide)")
            if host == org.calendar:
                raise ValueError(f"organizations.{key}: scheduling_calendar must differ from calendar")
        return self


class Calendar(Module[CalendarSettings]):
    name = "calendar"
    Settings = CalendarSettings

    api: GoogleCalendar | None = None

    async def start(self) -> None:
        if self.api is None:
            s = self.settings
            if not (s.client_id and s.client_secret and s.refresh_token):
                raise CalendarError(
                    "calendar needs client_id, client_secret and refresh_token; "
                    "set the first two and run `uv run agent-mcp-google-auth` for the third"
                )
            self.api = GoogleCalendar(
                oauth_token_provider(s.client_id, s.client_secret.get_secret_value(), s.refresh_token.get_secret_value())
            )
        self._calendars: list[dict[str, Any]] = []
        self._calendars_at = 0.0

    async def stop(self) -> None:
        if self.api is not None:
            await self.api.close()

    # ---- reading -------------------------------------------------------------------------

    @tool(read_only=True)
    async def calendars(self) -> list[dict[str, Any]]:
        """List every calendar on the account, hidden ones included, with ID, name and your access role."""
        return [
            {
                "id": c["id"],
                "name": c.get("summaryOverride") or c.get("summary"),
                "access_role": c.get("accessRole"),
                "hidden": c.get("hidden", False),
                "primary": c.get("primary", False),
                "time_zone": c.get("timeZone"),
            }
            for c in await self._calendar_list(refresh=True)
        ]

    @tool(read_only=True)
    async def events(
        self, calendar: str, start: str, end: str, query: str | None = None, limit: int = 250
    ) -> list[dict[str, Any]]:
        """List events in a calendar between start and end (ISO date or datetime; no offset means the
        configured time zone). Recurring events are expanded. `calendar` is a name or ID. `query`
        is Google's free-text search over title, description, location and attendees."""
        cal = await self._resolve(calendar)
        items = await self._api.events(cal["id"], self._rfc3339(start), self._rfc3339(end), query, limit)
        return [_summary(e) for e in items]

    @tool(read_only=True)
    async def event(self, calendar: str, event_id: str) -> dict[str, Any]:
        """Get one event with every field Google returns."""
        cal = await self._resolve(calendar)
        return await self._api.event(cal["id"], event_id)

    @tool(read_only=True)
    def organizations(self) -> dict[str, dict[str, Any]]:
        """Configured organizations: which calendar each one invites, where its events are listed,
        what to ignore, and any extra instructions. Use the key with `schedule`."""
        return {
            key: {
                "name": org.name or key,
                "calendar": org.calendar,
                "scheduling_calendar": self._host_name(org),
                "sources": org.sources,
                "ignore": org.ignore,
                "instructions": org.instructions,
                "estimated_end_prefix": org.estimated_end_prefix,
                "default_duration_minutes": org.default_duration_minutes,
            }
            for key, org in self.settings.organizations.items()
        }

    # ---- the one write -------------------------------------------------------------------

    @tool
    async def schedule(
        self,
        organization: str,
        title: str,
        start: str,
        end: str | None = None,
        description: str = "",
        location: str = "",
        source_url: str | None = None,
        end_estimated: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Offer an event to an organization's calendar as a pending invitation.

        Creates the event only in the organization's scheduling calendar and invites only the
        organization's calendar, leaving its RSVP as needsAction. Nothing else is ever written.

        start/end: ISO datetime (no offset means the configured time zone) or a date for an
        all-day event. If the source gives no clear end time, leave `end` out or set
        `end_estimated`; the title then gets the organization's prefix (e.g. "[#]") and the
        description says the end was estimated. `source_url` identifies the event at its source
        and is used to avoid duplicates; pass it whenever you have one.

        Returns status "created", "duplicate" (already in either calendar; nothing written),
        "ignored" (on the ignore list; nothing written) or "dry_run" (what would be created).
        """
        org = self.settings.organizations.get(organization)
        if org is None:
            raise ValueError(f"unknown organization {organization!r} (known: {sorted(self.settings.organizations)})")

        for pattern in org.ignore:
            if matches_pattern(title, pattern):
                return {"status": "ignored", "pattern": pattern}

        start_v = self._parse(start)
        all_day = isinstance(start_v, date) and not isinstance(start_v, datetime)
        if end is None:
            end_estimated = True
            end_v = start_v + (timedelta(days=1) if all_day else timedelta(minutes=org.default_duration_minutes))
        else:
            end_v = self._parse(end)
        if type(end_v) is not type(start_v):
            raise ValueError("start and end must both be dates or both be datetimes")
        if end_v <= start_v:
            raise ValueError("end must be after start")

        host = await self._resolve(self._host_name(org))
        invitee = await self._resolve(org.calendar)
        if host["id"] == invitee["id"]:
            raise ValueError("scheduling calendar and organization calendar are the same calendar")
        if host.get("accessRole") not in WRITABLE:
            raise ValueError(f"no write access to scheduling calendar {host['id']!r}")

        if dup := await self._find_duplicate(host, invitee, title, start_v, source_url):
            return {"status": "duplicate", **dup}

        if end_estimated:
            if not title.startswith(org.estimated_end_prefix):
                title = f"{org.estimated_end_prefix} {title}"
            description = f"{description}\n\n{org.estimated_end_note}".strip()

        private = {ORG_KEY: organization}
        if source_url:
            private[SOURCE_KEY] = source_url
        body: dict[str, Any] = {
            "summary": title,
            "description": description,
            "location": location,
            "start": self._when(start_v),
            "end": self._when(end_v),
            "attendees": [{"email": invitee["id"], "responseStatus": "needsAction"}],
            "guestsCanInviteOthers": False,
            "extendedProperties": {"private": private},
        }
        if source_url:
            body["source"] = {"title": org.name or organization, "url": source_url}

        if dry_run:
            return {"status": "dry_run", "calendar": host["id"], "event": body}
        created = await self._api.insert_event(host["id"], body, org.send_updates)
        return {"status": "created", "calendar": host["id"], "event": _summary(created)}

    # ---- helpers -------------------------------------------------------------------------

    @property
    def _api(self) -> GoogleCalendar:
        assert self.api is not None, "module not started"
        return self.api

    def _host_name(self, org: Organization) -> str:
        host = org.scheduling_calendar or self.settings.scheduling_calendar
        assert host is not None  # enforced by CalendarSettings
        return host

    async def _calendar_list(self, refresh: bool = False) -> list[dict[str, Any]]:
        if refresh or time.monotonic() - self._calendars_at > CALENDAR_CACHE_SECONDS:
            self._calendars = await self._api.calendars()
            self._calendars_at = time.monotonic()
        return self._calendars

    async def _resolve(self, ref: str) -> dict[str, Any]:
        """A calendar by ID, else by exact name, else by case-insensitive name. Ambiguity is an error."""
        for refresh in (False, True):
            cals = await self._calendar_list(refresh)
            for match in (
                lambda c: c["id"] == ref,
                lambda c: ref in (c.get("summaryOverride"), c.get("summary")),
                lambda c: ref.casefold() in {(c.get(k) or "").casefold() for k in ("summaryOverride", "summary")},
            ):
                found = [c for c in cals if match(c)]
                if len(found) == 1:
                    return found[0]
                if len(found) > 1:
                    raise CalendarError(f"calendar {ref!r} is ambiguous: {[c['id'] for c in found]}")
        raise CalendarError(f"no calendar named {ref!r}")

    async def _find_duplicate(
        self, host: dict[str, Any], invitee: dict[str, Any], title: str, start: date, source_url: str | None
    ) -> dict[str, Any] | None:
        if source_url:
            far_past, far_future = self._rfc3339("1970-01-01"), self._rfc3339("2100-01-01")
            prop = f"{SOURCE_KEY}={source_url}"
            for e in await self._api.events(host["id"], far_past, far_future, private_property=prop, limit=1):
                return {"calendar": host["id"], "reason": "same source_url", "event": _summary(e)}

        day = start.astimezone(self._tz).date() if isinstance(start, datetime) else start
        lo, hi = self._rfc3339(day.isoformat()), self._rfc3339((day + timedelta(days=1)).isoformat())
        for cal in (invitee, host):
            for e in await self._api.events(cal["id"], lo, hi):
                if e.get("status") != "cancelled" and similar(title, e.get("summary", "")):
                    return {"calendar": cal["id"], "reason": "similar title on the same day", "event": _summary(e)}
        return None

    @property
    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    def _parse(self, s: str) -> date:
        """An ISO date stays a date; a datetime gets the configured zone if it has none."""
        if len(s) == 10:
            return date.fromisoformat(s)
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=self._tz)

    def _rfc3339(self, s: str) -> str:
        v = self._parse(s)
        if not isinstance(v, datetime):
            v = datetime(v.year, v.month, v.day, tzinfo=self._tz)
        return v.isoformat()

    def _when(self, v: date) -> dict[str, str]:
        if isinstance(v, datetime):
            return {"dateTime": v.isoformat(), "timeZone": self.settings.timezone}
        return {"date": v.isoformat()}


def _summary(e: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in {
            "id": e.get("id"),
            "title": e.get("summary"),
            "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"),
            "end": e.get("end", {}).get("dateTime") or e.get("end", {}).get("date"),
            "location": e.get("location"),
            "description": e.get("description"),
            "status": e.get("status"),
            "organizer": e.get("organizer", {}).get("email"),
            "attendees": [
                {"email": a.get("email"), "response": a.get("responseStatus")} for a in e.get("attendees", [])
            ]
            or None,
            "recurring_event_id": e.get("recurringEventId"),
            "link": e.get("htmlLink"),
        }.items()
        if v is not None
    }
