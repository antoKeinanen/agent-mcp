import json
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from mcp import Client

from agent_mcp.config import Config, ConfigError, plan
from agent_mcp.modules.calendar import Calendar
from agent_mcp.modules.calendar.google import GoogleCalendar
from agent_mcp.server import build

HOST = "host@group.calendar.google.com"
ORG = "org@group.calendar.google.com"
OTHER = "me@gmail.com"


class FakeGoogle:
    """In-memory Calendar API. Records every non-GET request."""

    def __init__(self) -> None:
        self.calendars = [
            {"id": HOST, "summary": "ai scheduling", "accessRole": "owner", "hidden": True},
            {"id": ORG, "summary": "TKO-äly", "accessRole": "owner"},
            {"id": OTHER, "summary": "Anto", "accessRole": "owner", "primary": True},
        ]
        self.events: dict[str, list[dict[str, Any]]] = {HOST: [], ORG: [], OTHER: []}
        self.writes: list[httpx.Request] = []

    def add(self, cal: str, summary: str, start: str, end: str, **extra: Any) -> None:
        n = sum(len(v) for v in self.events.values())
        self.events[cal].append(
            {"id": f"e{n}", "summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end}, **extra}
        )

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = unquote(req.url.path.removeprefix("/calendar/v3"))
        if req.method != "GET":
            self.writes.append(req)
        if path == "/users/me/calendarList":
            return httpx.Response(200, json={"items": self.calendars})
        parts = path.split("/")  # ['', 'calendars', id, 'events', (event_id)]
        cal = parts[2]
        if req.method == "POST" and len(parts) == 4:
            body = json.loads(req.content)
            body["id"] = f"new{len(self.writes)}"
            self.events[cal].append(body)
            return httpx.Response(200, json=body)
        if req.method == "GET" and len(parts) == 4:
            items = self.events[cal]
            if prop := req.url.params.get("privateExtendedProperty"):
                k, v = prop.split("=", 1)
                items = [e for e in items if e.get("extendedProperties", {}).get("private", {}).get(k) == v]
            else:
                lo, hi = req.url.params["timeMin"], req.url.params["timeMax"]
                items = [e for e in items if lo[:10] <= e["start"]["dateTime"][:10] < hi[:10]]
            return httpx.Response(200, json={"items": items})
        if len(parts) == 5:
            ev = next((e for e in self.events[cal] if e["id"] == parts[4]), None)
            if ev is None:
                return httpx.Response(404, json={"error": "not found"})
            if req.method == "GET":
                return httpx.Response(200, json=ev)
            if req.method == "PATCH":
                ev.update(json.loads(req.content))
                return httpx.Response(200, json=ev)
            if req.method == "DELETE":
                self.events[cal].remove(ev)
                return httpx.Response(204)
        return httpx.Response(404, json={})


SETTINGS = {
    "scheduling_calendar": "ai scheduling",
    "organizations": {
        "tko-aly": {
            "name": "TKO-äly",
            "calendar": "TKO-äly",
            "sources": ["https://members.tko-aly.fi"],
            "ignore": ["gurula smash", "board game weekly"],
        }
    },
}


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


def client(
    google: FakeGoogle,
    tools: list[str] | None = None,
    settings: dict | None = None,
    feed: httpx.MockTransport | None = None,
) -> Client:
    cfg = Config.model_validate(
        {"modules": {"calendar": {"tools": tools or ["*"], "settings": settings or SETTINGS}}}
    )
    exposed = plan(cfg, {"calendar": Calendar})

    async def token() -> str:
        return "t"

    exposed[0].module.api = GoogleCalendar(token, httpx.MockTransport(google.handle))
    if feed is not None:
        exposed[0].module.http = httpx.AsyncClient(transport=feed)
    return Client(build(cfg, exposed))


async def call(c: Client, tool: str, **args: Any) -> Any:
    r = await c.call_tool(f"calendar_{tool}", args)
    assert not r.is_error, r.content
    return r.structured_content.get("result", r.structured_content)


async def test_reads_any_calendar_by_name(google: FakeGoogle) -> None:
    google.add(OTHER, "Dentist", "2026-11-20T10:00:00+02:00", "2026-11-20T11:00:00+02:00")
    async with client(google) as c:
        names = {cal["name"] for cal in await call(c, "calendars")}
        assert names == {"ai scheduling", "TKO-äly", "Anto"}
        events = await call(c, "events", calendar="anto", start="2026-11-01", end="2026-12-01")
        assert [e["title"] for e in events] == ["Dentist"]
        assert (await call(c, "event", calendar=OTHER, event_id=events[0]["id"]))["summary"] == "Dentist"
    assert google.writes == []


async def test_schedule_creates_only_in_host_and_invites_only_org(google: FakeGoogle) -> None:
    async with client(google) as c:
        r = await call(
            c, "schedule", organization="tko-aly", title="Fuksisitsit", start="2026-11-20T18:00",
            end="2026-11-20T23:00", description="Sitsit!", location="Gurula",
            source_url="https://members.tko-aly.fi/calendar/1",
        )
    assert r["status"] == "created"
    [req] = google.writes
    assert req.method == "POST" and unquote(req.url.path).endswith(f"/calendars/{HOST}/events")
    assert req.url.params["sendUpdates"] == "none"
    body = json.loads(req.content)
    assert body["attendees"] == [{"email": ORG, "responseStatus": "needsAction"}]
    assert body["summary"] == "Fuksisitsit"
    assert body["start"] == {"dateTime": "2026-11-20T18:00:00+02:00", "timeZone": "Europe/Helsinki"}
    assert body["location"] == "Gurula"


async def test_missing_end_is_estimated_and_marked(google: FakeGoogle) -> None:
    async with client(google) as c:
        r = await call(c, "schedule", organization="tko-aly", title="Excursion", start="2026-11-21T12:00")
    body = json.loads(google.writes[0].content)
    assert r["status"] == "created"
    assert body["summary"] == "[#] Excursion"
    assert body["end"]["dateTime"] == "2026-11-21T14:00:00+02:00"
    assert "estimated" in body["description"]


@pytest.mark.parametrize("title", ["Gurula Smash", "Board games weekly!", "[#] gurula smash bros"])
async def test_ignored_titles_are_not_written(google: FakeGoogle, title: str) -> None:
    async with client(google) as c:
        r = await call(c, "schedule", organization="tko-aly", title=title, start="2026-11-20T18:00")
    assert r["status"] == "ignored"
    assert google.writes == []


@pytest.mark.parametrize("cal", [ORG, HOST])
async def test_existing_events_are_duplicates(google: FakeGoogle, cal: str) -> None:
    google.add(cal, "[#] Fuksisitsit 2026", "2026-11-20T18:00:00+02:00", "2026-11-20T23:00:00+02:00")
    async with client(google) as c:
        r = await call(c, "schedule", organization="tko-aly", title="Fuksisitsit 2026", start="2026-11-20T19:00")
    assert r["status"] == "duplicate" and r["calendar"] == cal
    assert google.writes == []


async def test_same_source_url_is_a_duplicate_even_if_renamed(google: FakeGoogle) -> None:
    src = "https://members.tko-aly.fi/calendar/7"
    async with client(google) as c:
        await call(c, "schedule", organization="tko-aly", title="Talk", start="2026-11-20T18:00", source_url=src)
        r = await call(c, "schedule", organization="tko-aly", title="Guest lecture", start="2026-11-27T18:00",
                       source_url=src)
    assert r["status"] == "duplicate"
    assert len(google.writes) == 1


async def test_dry_run_writes_nothing(google: FakeGoogle) -> None:
    async with client(google) as c:
        r = await call(c, "schedule", organization="tko-aly", title="Talk", start="2026-11-20T18:00", dry_run=True)
    assert r["status"] == "dry_run" and r["calendar"] == HOST
    assert google.writes == []


async def test_unknown_org_is_rejected(google: FakeGoogle) -> None:
    async with client(google) as c:
        r = await c.call_tool("calendar_schedule", {"organization": "x", "title": "T", "start": "2026-11-20T18:00"})
    assert r.is_error
    assert google.writes == []


async def test_read_only_allowlist_hides_schedule(google: FakeGoogle) -> None:
    async with client(google, tools=["calendars", "events", "event", "organizations"]) as c:
        assert "calendar_schedule" not in {t.name for t in (await c.list_tools()).tools}


def test_org_cannot_invite_its_own_scheduling_calendar() -> None:
    bad = {"organizations": {"o": {"calendar": "ai scheduling", "scheduling_calendar": "ai scheduling"}}}
    cfg = Config.model_validate({"modules": {"calendar": {"tools": ["*"], "settings": bad}}})
    with pytest.raises(ConfigError, match="must differ"):
        plan(cfg, {"calendar": Calendar})


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"start": "2026-11-20T18:00", "end": "2026-11-20T17:00"}, "end must be after start"),
        ({"start": "next friday"}, "not an ISO date"),
        ({"organization": "nope", "start": "2026-11-20T18:00"}, "unknown organization 'nope'"),
    ],
)
async def test_errors_reach_the_agent(google: FakeGoogle, args: dict, message: str) -> None:
    async with client(google) as c:
        r = await c.call_tool("calendar_schedule", {"organization": "tko-aly", "title": "T", **args})
    assert r.is_error and message in r.content[0].text
    assert google.writes == []


def tagged(org: str = "tko-aly") -> dict[str, Any]:
    return {"extendedProperties": {"private": {"agent_mcp_org": org}}}


async def schedule(c: Client, **args: Any) -> str:
    r = await call(c, "schedule", organization="tko-aly", **({"title": "Talk", "start": "2026-11-20T18:00"} | args))
    assert r["status"] == "created"
    return r["event"]["id"]


async def test_update_changes_only_given_fields_and_keeps_attendees(google: FakeGoogle) -> None:
    async with client(google) as c:
        eid = await schedule(c, end="2026-11-20T20:00", location="Gurula")
        r = await call(c, "update", organization="tko-aly", event_id=eid, title="Guest talk", start="2026-11-21T17:00")
    assert r["status"] == "updated"
    req = google.writes[-1]
    assert req.method == "PATCH" and unquote(req.url.path).endswith(f"/calendars/{HOST}/events/{eid}")
    assert req.url.params["sendUpdates"] == "none"
    body = json.loads(req.content)
    assert set(body) == {"summary", "start", "end"}  # no attendees, location or tags
    assert body["start"]["dateTime"] == "2026-11-21T17:00:00+02:00"
    assert body["end"]["dateTime"] == "2026-11-21T19:00:00+02:00"  # length kept
    [ev] = google.events[HOST]
    assert ev["attendees"] == [{"email": ORG, "responseStatus": "needsAction"}]


async def test_real_end_clears_estimated_mark(google: FakeGoogle) -> None:
    async with client(google) as c:
        eid = await schedule(c, description="Fun")
        assert google.events[HOST][0]["summary"] == "[#] Talk"
        await call(c, "update", organization="tko-aly", event_id=eid, end="2026-11-20T21:00")
    ev = google.events[HOST][0]
    assert ev["summary"] == "Talk" and ev["description"] == "Fun"
    assert ev["end"]["dateTime"] == "2026-11-20T21:00:00+02:00"


async def test_end_estimated_marks_once(google: FakeGoogle) -> None:
    async with client(google) as c:
        eid = await schedule(c, end="2026-11-20T20:00")
        await call(c, "update", organization="tko-aly", event_id=eid, end_estimated=True)
        r = await call(c, "update", organization="tko-aly", event_id=eid, end_estimated=True)
    assert r["status"] == "unchanged"
    ev = google.events[HOST][0]
    assert ev["summary"] == "[#] Talk" and ev["description"].count("estimated") == 1


async def test_update_dry_run_and_no_op_write_nothing(google: FakeGoogle) -> None:
    async with client(google) as c:
        eid = await schedule(c, end="2026-11-20T20:00")
        dry = await call(c, "update", organization="tko-aly", event_id=eid, title="New", dry_run=True)
        same = await call(c, "update", organization="tko-aly", event_id=eid, title="Talk")
    assert dry["status"] == "dry_run" and dry["changes"] == {"summary": "New"}
    assert same["status"] == "unchanged"
    assert len(google.writes) == 1


async def test_delete_removes_own_event(google: FakeGoogle) -> None:
    async with client(google) as c:
        eid = await schedule(c)
        dry = await call(c, "delete", organization="tko-aly", event_id=eid, dry_run=True)
        assert dry["status"] == "dry_run" and len(google.writes) == 1
        r = await call(c, "delete", organization="tko-aly", event_id=eid)
    assert r["status"] == "deleted"
    assert google.writes[-1].method == "DELETE" and google.events[HOST] == []


@pytest.mark.parametrize(
    ("cal", "extra", "message"),
    [
        (HOST, {}, "not created by `schedule`"),  # put there by hand
        (HOST, tagged("other-org"), "not created by `schedule` for 'tko-aly'"),
        (HOST, tagged() | {"organizer": {"email": OTHER}}, "organized by"),  # someone else's invite
        (OTHER, tagged(), "no event"),  # the user's own calendar is never looked at
        (ORG, tagged(), "no event"),
    ],
)
@pytest.mark.parametrize("tool", ["update", "delete"])
async def test_only_events_schedule_created_can_change(
    google: FakeGoogle, cal: str, extra: dict, message: str, tool: str
) -> None:
    google.add(cal, "Dentist", "2026-11-20T10:00:00+02:00", "2026-11-20T11:00:00+02:00", **extra)
    eid = google.events[cal][0]["id"]
    async with client(google) as c:
        r = await c.call_tool(f"calendar_{tool}", {"organization": "tko-aly", "event_id": eid, "title": "x"}
                              if tool == "update" else {"organization": "tko-aly", "event_id": eid})
    assert r.is_error and message in r.content[0].text
    assert google.writes == []


async def test_update_rejects_bad_times(google: FakeGoogle) -> None:
    async with client(google) as c:
        eid = await schedule(c, end="2026-11-20T20:00")
        r = await c.call_tool("calendar_update", {"organization": "tko-aly", "event_id": eid, "end": "2026-11-20T17:00"})
    assert r.is_error and "end must be after start" in r.content[0].text
    assert len(google.writes) == 1
