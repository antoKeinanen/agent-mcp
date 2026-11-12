"""Machine-readable event sources for organizations.

A feed turns an organization's own event listing into `FeedEvent`s. It only reads. Add a
new one by writing an async function with the `Feed` signature and registering it in FEEDS.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import httpx


@dataclass(frozen=True)
class FeedEvent:
    id: str
    title: str
    start: datetime
    url: str
    end: datetime | None = None  # None when the source doesn't say
    description: str = ""
    location: str = ""
    category: str = ""


type Feed = Callable[[httpx.AsyncClient, str, datetime, datetime], Awaitable[list[FeedEvent]]]


# ---- TKO-äly ---------------------------------------------------------------------------

TKO_ALY_API = "https://event-api.tko-aly.fi/api/events"
TKO_ALY_EVENT_PAGE = "https://members.tko-aly.fi/calendar_events/view/{id}"
TKO_ALY_TZ = ZoneInfo("Europe/Helsinki")  # the API's times carry no offset


async def tko_aly(http: httpx.AsyncClient, url: str, start: datetime, end: datetime) -> list[FeedEvent]:
    """Events from event-api.tko-aly.fi. It has no end times. `fromDate` filters, `toDate` is ignored."""
    local_from = start.astimezone(TKO_ALY_TZ).date()
    r = await http.get(url, params={"fromDate": local_from.isoformat()})
    r.raise_for_status()
    events = []
    for e in r.json():
        if e.get("deleted"):
            continue
        starts = datetime.fromisoformat(e["starts"]).replace(tzinfo=TKO_ALY_TZ)
        if not start <= starts < end:
            continue
        page = TKO_ALY_EVENT_PAGE.format(id=e["id"])
        events.append(
            FeedEvent(
                id=str(e["id"]),
                title=(e.get("name") or "").strip(),
                start=starts,
                url=page,
                description=_tko_aly_description(e, page),
                location=(e.get("location") or "").strip(),
                category=(e.get("category") or "").strip(),
            )
        )
    return sorted(events, key=lambda ev: ev.start)


def _tko_aly_description(e: dict, page: str) -> str:
    body = html_to_text(e.get("description") or "")
    extra = []
    if price := (e.get("price") or "").strip():
        extra.append(f"Price: {price}")
    if org := (e.get("organizer") or {}).get("name"):
        extra.append(f"Organizer: {org}")
    for key, label in (("registration_starts", "Registration opens"), ("registration_ends", "Registration closes")):
        if e.get(key):
            extra.append(f"{label}: {_local(e[key])}")
    extra.append(page)
    return "\n\n".join(p for p in (body, "\n".join(extra)) if p)


def _local(naive: str) -> str:
    return datetime.fromisoformat(naive).strftime("%Y-%m-%d %H:%M")


FEEDS: dict[str, tuple[Feed, str]] = {"tko-aly": (tko_aly, TKO_ALY_API)}


# ---- HTML to plain text ----------------------------------------------------------------

_BLOCK = {"p", "div", "br", "hr", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table"}
_SKIP = {"script", "style"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.href: list[str | None] = []
        self.link_text: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")
        if tag == "a":
            self.href.append(dict(attrs).get("href"))
            self.link_text.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.out.append("\n")
        if tag == "a" and self.href:
            href, text = self.href.pop(), self.link_text.pop()
            if href and not href.startswith(("tel:", "mailto:")) and href.strip() != text.strip():
                self.out.append(f" ({href})")

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        self.out.append(data)
        if self.link_text:
            self.link_text[-1] += data


def html_to_text(s: str) -> str:
    """Readable text from an HTML snippet: block tags become line breaks, links keep their URL."""
    p = _Text()
    p.feed(s.replace("\r\n", "\n"))
    p.close()
    text = "".join(p.out)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def window(days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    start = now or datetime.now().astimezone()
    return start, start + timedelta(days=days)
