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

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from agent_mcp.module import Module, tool

from .feeds import FEEDS, FeedEvent, window
from .google import CalendarError, GoogleCalendar, oauth_token_provider
from .matching import matches_pattern, similar
from .signin import complete_sign_in, parse_code, refresh_token, sign_in_help

log = logging.getLogger("agent_mcp.calendar")

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
    feed: str | None = Field(None, description=f"Event feed `scan` reads. One of: {', '.join(FEEDS)}.")
    feed_url: str | None = Field(None, description="Override the feed's default URL.")
    instructions: str = Field("", description="Extra guidance handed to the agent with this organization.")
    ignore: list[str] = Field([], description="Titles never to schedule. Fuzzy-matched.")
    estimated_end_prefix: str = "[#]"
    estimated_end_note: str = "Note: the end time is estimated because the source did not clearly state one."
    default_duration_minutes: int = Field(120, gt=0, description="Used when no end time is given.")
    send_updates: Literal["none", "all", "externalOnly"] = "none"

    @model_validator(mode="after")
    def _known_feed(self) -> "Organization":
        if self.feed is not None and self.feed not in FEEDS:
            raise ValueError(f"unknown feed {self.feed!r} (known: {', '.join(FEEDS)})")
        return self


class CalendarSettings(_Strict):
    client_id: str | None = Field(None, description="Google OAuth client ID (Desktop app).")
    client_secret: SecretStr | None = None
    refresh_token: SecretStr | None = Field(None, description="Written by `agent-mcp-google-auth`, or leave unset.")
    auth_code: SecretStr | None = Field(
        None, description="Redirect URL (or code) from the sign-in link the server logs. Used once."
    )
    token_file: Path | None = Field(None, description="Where a refresh token from auth_code is kept.")
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
    http: httpx.AsyncClient | None = None  # feeds and the sign-in code exchange
    signed_out: str | None = None  # why `api` is missing, shown to the agent

    async def start(self) -> None:
        """Never fails for missing sign-in: the server stays up and the tools explain what to do."""
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=30, headers={"User-Agent": "agent-mcp"})
        self._calendars: list[dict[str, Any]] = []
        self._calendars_at = 0.0
        if self.api is not None:
            return
        s = self.settings
        if not (s.client_id and s.client_secret):
            self.signed_out = (
                "Google Calendar is not configured: set AGENT_MCP__CALENDAR__CLIENT_ID and "
                "AGENT_MCP__CALENDAR__CLIENT_SECRET (Google Cloud > Credentials > OAuth client ID > Desktop app)."
            )
        else:
            secret = s.client_secret.get_secret_value()
            try:
                token, warning = await refresh_token(
                    self.http,
                    s.client_id,
                    secret,
                    s.refresh_token.get_secret_value() if s.refresh_token else None,
                    s.token_file.expanduser() if s.token_file else None,
                    s.auth_code.get_secret_value() if s.auth_code else None,
                )
            except CalendarError as e:
                self.signed_out = str(e)
            except httpx.HTTPError as e:
                self.signed_out = sign_in_help(s.client_id, f"Could not reach Google to finish sign-in ({e!r}).")
            except Exception as e:  # never take the server down over sign-in
                log.exception("calendar: sign-in failed")
                self.signed_out = sign_in_help(s.client_id, f"Sign-in failed unexpectedly ({e!r}).")
            else:
                self._signed_in(token, warning)
                return
        log.warning("calendar: %s", self.signed_out)

    def _signed_in(self, token: str, warning: str | None) -> None:
        s = self.settings
        assert s.client_id and s.client_secret
        refused = sign_in_help(s.client_id, "Google refused the saved sign-in (revoked or expired).")
        self.api = GoogleCalendar(oauth_token_provider(s.client_id, s.client_secret.get_secret_value(), token, refused))
        self.signed_out = None
        if warning:
            log.warning("calendar: %s", warning)
        else:
            log.info("calendar: signed in to Google")

    async def stop(self) -> None:
        if self.api is not None:
            await self.api.close()
        if self.http is not None:
            await self.http.aclose()

    # ---- sign-in -------------------------------------------------------------------------

    @tool
    async def sign_in(self, redirect_url: str) -> dict[str, Any]:
        """Finish Google sign-in with the URL the user's browser landed on after allowing access
        (http://localhost/?code=..., a page that doesn't load). Call it as soon as the user gives
        you that URL: the code in it expires within minutes and works once. Only works while
        signed out; the error from any other calendar tool contains the sign-in link to give them.
        """
        s = self.settings
        if self.api is not None:
            raise CalendarError("Already signed in to Google. Nothing to do.")
        if not (s.client_id and s.client_secret):
            raise CalendarError(self.signed_out or "Set AGENT_MCP__CALENDAR__CLIENT_ID and CLIENT_SECRET first.")
        code = parse_code(redirect_url)
        if not code or code.startswith("http"):
            raise CalendarError(sign_in_help(s.client_id, "That URL has no ?code=... in it."))
        assert self.http is not None, "module not started"
        try:
            token, warning = await complete_sign_in(
                self.http,
                s.client_id,
                s.client_secret.get_secret_value(),
                code,
                s.token_file.expanduser() if s.token_file else None,
            )
        except httpx.HTTPError as e:
            raise CalendarError(f"Could not reach Google ({e!r}). Try again with the same URL.") from e
        self._signed_in(token, warning)
        return {"signed_in": True, "saved": warning is None, **({"warning": warning} if warning else {})}

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
                "feed": org.feed,
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
        org = self._org(organization)
        if pattern := _ignored(org, title):
            return {"status": "ignored", "pattern": pattern}

        start_v = self._parse(start)
        all_day = isinstance(start_v, date) and not isinstance(start_v, datetime)
        if end is None:
            end_estimated = True
            end_v = start_v + (timedelta(days=1) if all_day else timedelta(minutes=org.default_duration_minutes))
        else:
            end_v = self._parse(end)
        if type(end_v) is not type(start_v):
            raise CalendarError("start and end must both be dates or both be datetimes")
        if end_v <= start_v:
            raise CalendarError("end must be after start")

        host, invitee = await self._calendars_for(org)
        day = self._day(start_v)
        existing = await self._existing(
            host, invitee, day, day + timedelta(days=1), source=f"{SOURCE_KEY}={source_url}" if source_url else None
        )
        if dup := existing.match(title, start_v, source_url, self._day):
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

    @tool(read_only=True)
    async def scan(self, organization: str, days: int = 60) -> dict[str, Any]:
        """Read the organization's event feed for the next `days` days and check each event
        against its calendar, its scheduling calendar and its ignore list. Writes nothing.

        Each event comes back with a status:
          "missing"   - on neither calendar; pass its `schedule` arguments to `schedule`
          "duplicate" - already on a calendar (`existing` shows which event)
          "ignored"   - matches the ignore list (`pattern` shows which entry)
        Review "missing" ones before scheduling: the matching is fuzzy, so skip anything that is
        really the same as an existing or ignored event. Feeds without end times return no
        `end`. Add one only if the description clearly states it, otherwise `schedule` estimates it.
        """
        org = self._org(organization)
        if org.feed is None:
            raise CalendarError(f"organization {organization!r} has no feed; set `feed` (one of: {', '.join(FEEDS)})")
        if not 1 <= days <= 366:
            raise CalendarError("days must be between 1 and 366")
        fetch, default_url = FEEDS[org.feed]
        lo, hi = window(days)
        assert self.http is not None, "module not started"
        try:
            found = await fetch(self.http, org.feed_url or default_url, lo, hi)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise CalendarError(f"reading the {org.feed} feed failed: {e!r}") from e

        host, invitee = await self._calendars_for(org)
        existing = await self._existing(
            host, invitee, self._day(lo), self._day(hi) + timedelta(days=1), source=f"{ORG_KEY}={organization}"
        )
        events = []
        for fe in found:
            entry: dict[str, Any] = {"schedule": _schedule_args(fe), "category": fe.category or None}
            if pattern := _ignored(org, fe.title):
                entry |= {"status": "ignored", "pattern": pattern}
            elif dup := existing.match(fe.title, fe.start, fe.url, self._day):
                entry |= {"status": "duplicate", "reason": dup["reason"], "existing": dup["event"]}
            else:
                entry["status"] = "missing"
            events.append({k: v for k, v in entry.items() if v is not None})
        counts = {s: sum(e["status"] == s for e in events) for s in ("missing", "duplicate", "ignored")}
        return {
            "organization": organization,
            "feed": org.feed,
            "from": lo.isoformat(timespec="minutes"),
            "to": hi.isoformat(timespec="minutes"),
            "counts": counts,
            "events": events,
        }

    # ---- helpers -------------------------------------------------------------------------

    def _org(self, key: str) -> Organization:
        org = self.settings.organizations.get(key)
        if org is None:
            raise CalendarError(f"unknown organization {key!r} (known: {sorted(self.settings.organizations)})")
        return org

    async def _calendars_for(self, org: Organization) -> tuple[dict[str, Any], dict[str, Any]]:
        """The organization's (scheduling, invited) calendars, checked to be distinct and writable."""
        host = await self._resolve(self._host_name(org))
        invitee = await self._resolve(org.calendar)
        if host["id"] == invitee["id"]:
            raise CalendarError("scheduling calendar and organization calendar are the same calendar")
        if host.get("accessRole") not in WRITABLE:
            raise CalendarError(f"no write access to scheduling calendar {host['id']!r}")
        return host, invitee

    @property
    def _api(self) -> GoogleCalendar:
        if self.api is None:
            raise CalendarError(self.signed_out or "calendar module not started")
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

    async def _existing(
        self, host: dict[str, Any], invitee: dict[str, Any], lo: date, hi: date, source: str | None
    ) -> "_Existing":
        """Events on both calendars between local days lo and hi, plus every event on the
        scheduling calendar carrying the private property `source` (key=value), at any date."""
        by_day = [
            (cal["id"], e)
            for cal in (invitee, host)
            for e in await self._api.events(cal["id"], self._rfc3339(lo.isoformat()), self._rfc3339(hi.isoformat()))
            if e.get("status") != "cancelled"
        ]
        tagged = []
        if source:
            far_past, far_future = self._rfc3339("1970-01-01"), self._rfc3339("2100-01-01")
            tagged = await self._api.events(host["id"], far_past, far_future, private_property=source, limit=2500)
        return _Existing(host["id"], by_day, tagged)

    def _day(self, v: date) -> date:
        return v.astimezone(self._tz).date() if isinstance(v, datetime) else v

    @property
    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    def _parse(self, s: str) -> date:
        """An ISO date stays a date; a datetime gets the configured zone if it has none."""
        try:
            if len(s) == 10:
                return date.fromisoformat(s)
            dt = datetime.fromisoformat(s)
        except ValueError as e:
            raise CalendarError(f"not an ISO date or datetime: {s!r}") from e
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


@dataclass(frozen=True)
class _Existing:
    host_id: str
    by_day: list[tuple[str, dict[str, Any]]]  # (calendar id, event)
    tagged: list[dict[str, Any]]  # scheduled earlier by this module

    def match(self, title: str, start: date, source_url: str | None, day: Any) -> dict[str, Any] | None:
        if source_url:
            for e in self.tagged:
                if e.get("extendedProperties", {}).get("private", {}).get(SOURCE_KEY) == source_url:
                    return {"calendar": self.host_id, "reason": "same source_url", "event": _summary(e)}
        want = day(start)
        for cal_id, e in self.by_day:
            if similar(title, e.get("summary", "")) and _event_day(e, day) == want:
                return {"calendar": cal_id, "reason": "similar title on the same day", "event": _summary(e)}
        return None


def _event_day(e: dict[str, Any], day: Any) -> date | None:
    s = e.get("start", {})
    if "dateTime" in s:
        return day(datetime.fromisoformat(s["dateTime"]))
    return date.fromisoformat(s["date"]) if "date" in s else None


def _ignored(org: Organization, title: str) -> str | None:
    return next((p for p in org.ignore if matches_pattern(title, p)), None)


def _schedule_args(fe: FeedEvent) -> dict[str, Any]:
    args: dict[str, Any] = {"title": fe.title, "start": fe.start.isoformat()}
    if fe.end is not None:
        args["end"] = fe.end.isoformat()
    return args | {"description": fe.description, "location": fe.location, "source_url": fe.url}


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
