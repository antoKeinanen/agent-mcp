import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx
import pytest

from agent_mcp.config import Config, ConfigError, plan
from agent_mcp.modules.calendar import Calendar
from agent_mcp.modules.calendar.feeds import TKO_ALY_API, html_to_text, tko_aly
from test_calendar import HOST, ORG, SETTINGS, FakeGoogle, call, client

HEL = ZoneInfo("Europe/Helsinki")


def tko_event(id: int, name: str, starts: datetime, **extra: Any) -> dict[str, Any]:
    return {
        "id": id, "name": name, "starts": starts.strftime("%Y-%m-%dT%H:%M:%S"), "deleted": 0, "price": "",
        "location": "Gurula, DK115, Exactum", "category": "", "organizer": None, "description": "",
        "registration_starts": None, "registration_ends": None, **extra,
    }


class FakeTko:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.requests: list[httpx.Request] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert str(req.url).startswith(TKO_ALY_API)
        since = req.url.params.get("fromDate", "")
        return httpx.Response(200, json=[e for e in self.events if e["starts"] >= since])


def soon(days: int, hour: int = 18) -> datetime:
    """A naive Helsinki time `days` from now, as the API returns it."""
    d = datetime.now(HEL).date() + timedelta(days=days)
    return datetime(d.year, d.month, d.day, hour, 0)


FEED_SETTINGS = {
    **SETTINGS,
    "organizations": {"tko-aly": {**SETTINGS["organizations"]["tko-aly"], "feed": "tko-aly"}},
}


def test_html_to_text_keeps_structure_and_links() -> None:
    html = '<div style="x">🎲</div><hr>Tule <b>mukaan</b>!\r\n<a href="https://t.me/x">Telegram</a>' \
           '<ul><li>one</li><li>two</li></ul><a href="tel:+358">+358</a><script>alert(1)</script>'
    assert html_to_text(html) == "🎲\n\nTule mukaan!\nTelegram (https://t.me/x)\n\n- one\n\n- two\n\n+358"


async def test_tko_aly_feed_parses_and_filters() -> None:
    old = tko_event(1, "Past", soon(-3))
    gone = tko_event(2, "Deleted", soon(2), deleted=1)
    later = tko_event(3, "Far away", soon(100))
    talk = tko_event(
        4, " Talk ", soon(2, 17), price="0 €", registration_ends="2026-11-18T17:00:00",
        organizer={"name": "HYK & TKO-äly", "url": ""}, description="<p>Hello</p>",
    )
    fake = FakeTko([old, gone, later, talk])
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        now = datetime.now(UTC)
        [ev] = await tko_aly(http, TKO_ALY_API, now, now + timedelta(days=60))
    assert fake.requests[0].url.params["fromDate"] == datetime.now(HEL).date().isoformat()
    assert ev.id == "4" and ev.title == "Talk" and ev.end is None
    assert ev.start == soon(2, 17).replace(tzinfo=HEL)
    assert ev.url == "https://members.tko-aly.fi/calendar_events/view/4"
    assert ev.description == (
        "Hello\n\nPrice: 0 €\nOrganizer: HYK & TKO-äly\nRegistration closes: 2026-11-18 17:00\n"
        "https://members.tko-aly.fi/calendar_events/view/4"
    )


async def test_scan_classifies_and_writes_nothing() -> None:
    google = FakeGoogle()
    on_cal = soon(3).replace(tzinfo=HEL)
    google.add(ORG, "Sauna of Destiny", on_cal.isoformat(), (on_cal + timedelta(hours=3)).isoformat())
    tko = FakeTko([
        tko_event(10, "Sauna of Destiny", soon(3, 17)),
        tko_event(11, "Board Game Weekly", soon(4)),
        tko_event(12, "Gurula Smash Weekly #53", soon(5)),
        tko_event(13, "Pipsa Possu sitsit", soon(6)),
    ])
    async with client(google, settings=FEED_SETTINGS, feed=httpx.MockTransport(tko.handle)) as c:
        r = await call(c, "scan", organization="tko-aly")
    assert google.writes == []
    by_title = {e["schedule"]["title"]: e for e in r["events"]}
    assert r["counts"] == {"missing": 1, "duplicate": 1, "ignored": 2}
    assert by_title["Sauna of Destiny"]["status"] == "duplicate"
    assert by_title["Sauna of Destiny"]["existing"]["title"] == "Sauna of Destiny"
    assert by_title["Board Game Weekly"]["pattern"] == "board game weekly"
    assert by_title["Gurula Smash Weekly #53"]["pattern"] == "gurula smash"
    missing = by_title["Pipsa Possu sitsit"]
    assert missing["status"] == "missing" and "end" not in missing["schedule"]


async def test_scanned_event_schedules_once_then_scans_as_duplicate() -> None:
    google = FakeGoogle()
    tko = FakeTko([tko_event(20, "Excursion", soon(7, 12))])
    async with client(google, settings=FEED_SETTINGS, feed=httpx.MockTransport(tko.handle)) as c:
        [first] = (await call(c, "scan", organization="tko-aly"))["events"]
        created = await call(c, "schedule", organization="tko-aly", **first["schedule"])
        tko.events[0]["name"] = "Excursion (renamed)"  # same source_url still catches it
        tko.events[0]["starts"] = soon(8, 12).strftime("%Y-%m-%dT%H:%M:%S")
        [again] = (await call(c, "scan", organization="tko-aly"))["events"]
    assert created["status"] == "created"
    body = json.loads(google.writes[0].content)
    assert body["summary"] == "[#] Excursion"
    assert body["attendees"] == [{"email": ORG, "responseStatus": "needsAction"}]
    assert body["source"]["url"] == "https://members.tko-aly.fi/calendar_events/view/20"
    assert again["status"] == "duplicate" and again["reason"] == "same source_url"
    assert len(google.writes) == 1 and HOST in unquote(str(google.writes[0].url))


async def test_scan_needs_a_feed() -> None:
    async with client(FakeGoogle()) as c:
        r = await c.call_tool("calendar_scan", {"organization": "tko-aly"})
    assert r.is_error and "no feed" in r.content[0].text


def test_unknown_feed_is_rejected_at_startup() -> None:
    bad = {**SETTINGS, "organizations": {"o": {"calendar": "x", "feed": "nope"}}}
    cfg = Config.model_validate({"modules": {"calendar": {"tools": ["*"], "settings": bad}}})
    with pytest.raises(ConfigError, match="unknown feed 'nope'"):
        plan(cfg, {"calendar": Calendar})
