import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
from havasu.newbook import BOOKING_PAGE

FIXTURES = Path(__file__).parent / "fixtures"

#: Captured 2026-11-16. March-November 2026: every day booked until Nov 16, then
#: Nov 16-30 available (Nov 16 also `closed_departure`).
CAL_2026 = "calendar-2026-03-to-2026-11.json"
#: Captured 2026-11-16. December 2026-December 2027, all booked (not on sale yet).
CAL_2027 = "calendar-2026-12-to-2027-12.json"
#: Captured 2026-11-16, the `all`-mode query. Nov 16-30 available, everything later
#: booked, and Dec 1 carries `closed_arrival` - which it does only when the query
#: spans the Nov/Dec boundary.
CAL_ALL = "calendar-2026-11-to-2027-12.json"

API_PATH = (
    "https://bookingsus.newbook.cloud/online/generated_api_files/"
    "a2c34c2a209b28633a19718581677b9e.php?"
)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(fixture_text(name))
    return loaded


def with_rows(payload: dict[str, Any], rows: str) -> dict[str, Any]:
    return {**payload, "calendar_data": {"table_rows": rows}}


class FakeNewBook:
    """Serves the booking page and the calendar, recording what was asked."""

    def __init__(self, calendar: httpx.Response | None = None) -> None:
        self.page_loads = 0
        self.forms: list[dict[str, list[str]]] = []
        self.cookies_sent: list[str | None] = []
        self.calendar = calendar or httpx.Response(200, json=fixture_json(CAL_2026))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and str(request.url) == BOOKING_PAGE:
            self.page_loads += 1
            self.cookies_sent.append(request.headers.get("cookie"))
            return httpx.Response(
                200, text=fixture_text("booking-page.html"), headers={"set-cookie": "nb=1"}
            )
        assert request.method == "POST"
        assert str(request.url) == f"{API_PATH}newbook_api_action=data"
        self.forms.append(parse_qs(request.content.decode()))
        return self.calendar
