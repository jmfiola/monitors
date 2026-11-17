"""The NewBook public booking calendar for Havasupai campground permits.

No account is involved. The booking page sets a session cookie and names a
generated API path; one POST to that path returns per-night availability for any
span of months. Nothing here ever calls a `cart` action: adding to a cart can hold
a permit away from the people trying to book it.
"""

from __future__ import annotations

import calendar
import json
import re
from collections.abc import Mapping
from datetime import date, timedelta
from html.parser import HTMLParser

import httpx

from havasu.detector import months_between
from havasu.types import NIGHTS, DayState, YearMonth

BOOKING_PAGE = "https://bookingsus.newbook.cloud/online/havasupai"
_ORIGIN = "https://bookingsus.newbook.cloud"
#: "Campground Permits" on the booking page. Lodge rooms are a different category.
CAMPGROUND_CATEGORY_ID = "1"

#: The path's file name is a generated hash, so it is read from the page on every
#: bootstrap rather than written down here.
_API_PATH = re.compile(
    r"newbook_api_path='(https://bookingsus\.newbook\.cloud/online/generated_api_files/"
    r"[0-9a-f]+\.php\?)'"
)

HEADERS: dict[str, str] = {
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
}
#: What the page's own jQuery sends with the calendar request.
_XHR_HEADERS: dict[str, str] = {
    **HEADERS,
    "accept": "application/json, text/javascript, */*; q=0.01",
    "origin": _ORIGIN,
    "referer": BOOKING_PAGE,
    "x-requested-with": "XMLHttpRequest",
}

_STATE_TOKENS = frozenset({"booked", "available"})
_MODIFIER_TOKENS = frozenset({"closed_arrival", "closed_departure"})


class NewBookError(RuntimeError):
    """The calendar could not be read, or did not look like the one discovery saw.

    A fault, not a busy signal: the runner backs off and leaves the baseline alone,
    because a calendar that failed to parse is not a calendar with nothing open.
    """


class _CalendarParser(HTMLParser):
    """Collects `<td class="day …" data-date="YYYY-MM-DD">` cells, strictly.

    `<td class="empty">` pads a week row and is skipped. Any other cell shape, any
    class token not seen during discovery, and any repeated date are faults: a
    misread here would announce, or silently swallow, a permit.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.days: dict[date, DayState] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "td":
            return
        attributes = dict(attrs)
        tokens = (attributes.get("class") or "").split()
        raw_date = attributes.get("data-date")
        if raw_date is None:
            if tokens == ["empty"]:
                return
            raise NewBookError(f"calendar cell without a date: class={tokens}")
        if not tokens or tokens[0] != "day":
            raise NewBookError(f"calendar cell {raw_date} has unexpected class={tokens}")

        states = [t for t in tokens[1:] if t in _STATE_TOKENS]
        unknown = [t for t in tokens[1:] if t not in _STATE_TOKENS | _MODIFIER_TOKENS]
        if unknown:
            raise NewBookError(f"calendar cell {raw_date} has unknown class token(s) {unknown}")
        if len(states) != 1:
            raise NewBookError(f"calendar cell {raw_date} has state tokens {states}")

        try:
            day = date.fromisoformat(raw_date)
        except ValueError:
            raise NewBookError(f"calendar cell has an unparseable date {raw_date!r}") from None
        if day in self.days:
            raise NewBookError(f"calendar repeats {raw_date}")
        self.days[day] = DayState(
            available=states[0] == "available",
            closed_arrival="closed_arrival" in tokens,
            closed_departure="closed_departure" in tokens,
        )


def parse_calendar(payload: object, first: YearMonth, last: YearMonth) -> dict[date, DayState]:
    """The calendar response as one state per day, for exactly the requested months.

    The echoed period must match the request and every day of every month must be
    present exactly once. A short answer is a fault rather than a partial result:
    banking a missing month as "nothing open" would re-alert all of it on the next
    good read.
    """
    if not isinstance(payload, Mapping):
        raise NewBookError("calendar response is not a JSON object")
    echoed = (payload.get("period_from"), payload.get("period_to"))
    if echoed != (str(first), str(last)):
        raise NewBookError(f"calendar answered for {echoed}, asked for {(str(first), str(last))}")
    data = payload.get("calendar_data")
    rows = data.get("table_rows") if isinstance(data, Mapping) else None
    if not isinstance(rows, str):
        raise NewBookError("calendar response has no table_rows")

    parser = _CalendarParser()
    parser.feed(rows)
    parser.close()

    expected = {
        date(m.year, m.month, d)
        for m in months_between(first, last)
        for d in range(1, calendar.monthrange(m.year, m.month)[1] + 1)
    }
    got = set(parser.days)
    if got != expected:
        missing = sorted(expected - got)
        extra = sorted(got - expected)
        raise NewBookError(
            f"calendar covers the wrong days: {len(missing)} missing "
            f"(first {missing[:1]}), {len(extra)} unexpected (first {extra[:1]})"
        )
    return parser.days


def _form_date(day: date) -> str:
    """The booking form's own date format, e.g. "Nov 16 2026"."""
    return f"{day:%b} {day.day} {day.year}"


class NewBookSource:
    """An anonymous NewBook session plus the one calendar call.

    The session is dropped on ANY failure, so the next tick bootstraps afresh. That
    costs one extra page load after a fault and covers an expired cookie, a rotated
    API path and an outage the same way, without guessing which one it was.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._api_path: str | None = None

    async def _bootstrap(self) -> str:
        response = await self._client.get(BOOKING_PAGE, headers=HEADERS, follow_redirects=True)
        if response.status_code != 200:
            raise NewBookError(f"booking page failed: HTTP {response.status_code}")
        match = _API_PATH.search(response.text)
        if match is None:
            raise NewBookError("booking page no longer names a newbook_api_path")
        return match.group(1)

    async def fetch_calendar(
        self, first: YearMonth, last: YearMonth, available_from: date
    ) -> dict[date, DayState]:
        try:
            if self._api_path is None:
                self._api_path = await self._bootstrap()
            response = await self._client.post(
                f"{self._api_path}newbook_api_action=data",
                headers=_XHR_HEADERS,
                data={
                    "available_from": _form_date(available_from),
                    "available_to": _form_date(available_from + timedelta(days=NIGHTS)),
                    "nights": str(NIGHTS),
                    # One permit per booking: the chart refuses adults > 1 on this
                    # rate, and the calendar answers identically for 1, 4 and 12.
                    "adults": "1",
                    "query": "newbook_calendar_update_table_dates",
                    "category_id": CAMPGROUND_CATEGORY_ID,
                    "look_up_date": str(first),
                    "period_from": str(first),
                    "period_to": str(last),
                },
            )
            if response.status_code != 200:
                raise NewBookError(f"calendar request failed: HTTP {response.status_code}")
            try:
                payload = response.json()
            except json.JSONDecodeError:
                raise NewBookError("calendar response is not JSON") from None
            return parse_calendar(payload, first, last)
        except Exception:
            self._api_path = None
            raise
