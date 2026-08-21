import json
from pathlib import Path

import httpx
import pytest
from melanzana.cowlendar import (
    CowlendarError,
    build_availability_url,
    fetch_month,
    parse_availability,
)
from melanzana.types import Slot

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "availability-sample.json").read_text(encoding="utf-8")
)

# Byte-for-byte what the TypeScript's URLSearchParams emits for these inputs.
EXPECTED_URL = (
    "https://app.cowlendar.com/extapi/calendar/CAL123/availability"
    "?year=2026&month=12&timezone=America%2FDenver"
    "&quantity_details%5B0%5D%5Btype%5D=default"
    "&quantity_details%5B0%5D%5Bquantity%5D=1"
    "&quantity_details%5B0%5D%5Bname%5D=Default"
    "&teammate_id=all&duration=30&is_manual=false&is_pos=false&variant_id=VAR456"
)


def test_the_availability_url_is_byte_identical_to_the_typescript_one() -> None:
    assert build_availability_url("CAL123", "VAR456", "America/Denver", 2026, 12) == EXPECTED_URL


def test_parse_maps_the_long_array_to_slots() -> None:
    slots = parse_availability(FIXTURE)
    assert len(slots) == 3
    assert slots[0] == Slot(
        key="2026-12-01 10:30", start_unix=1796146200, qty_left=4, is_bookable=True
    )
    assert [s.key for s in slots] == [
        "2026-12-01 10:30",
        "2026-12-01 11:00",
        "2026-12-01 11:30",
    ]


def test_parse_returns_nothing_when_long_is_missing_or_not_an_array() -> None:
    assert parse_availability({}) == []
    assert parse_availability({"long": None}) == []
    assert parse_availability({"long": "nope"}) == []
    assert parse_availability("not an object") == []


def test_parse_skips_unusable_rows_rather_than_failing_the_poll() -> None:
    # A garbage row is a row, not an outage. Dropping it keeps the other slots
    # alertable; raising would turn one malformed entry into a stalled monitor.
    payload = {
        "long": [
            "not an object",
            {"slot": "", "slot_start_unix": 1796146200},
            {"slot": "2026-12-01 10:30", "slot_start_unix": 0},
            {"slot": "2026-12-01 11:00", "slot_start_unix": "garbage"},
            {"slot": "2026-12-01 11:30", "slot_start_unix": 1796149800},
        ]
    }
    assert [s.key for s in parse_availability(payload)] == ["2026-12-01 11:30"]


def test_parse_defaults_a_missing_quantity_and_flag() -> None:
    payload = {"long": [{"slot": "2026-12-01 10:30", "slot_start_unix": 1796146200}]}
    slot = parse_availability(payload)[0]
    assert slot.qty_left == 0
    assert slot.is_bookable is False


async def test_fetch_month_returns_parsed_slots() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, json=FIXTURE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        slots = await fetch_month("CAL123", "VAR456", "America/Denver", 2026, 12, client)

    assert [s.key for s in slots] == [
        "2026-12-01 10:30",
        "2026-12-01 11:00",
        "2026-12-01 11:30",
    ]
    assert seen["url"] == EXPECTED_URL
    assert "Chrome" in seen["ua"]  # the widget's own headers, to reduce block risk


async def test_fetch_month_raises_on_a_non_ok_response() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(429))
    ) as client:
        with pytest.raises(CowlendarError, match="429"):
            await fetch_month("CAL123", "VAR456", "America/Denver", 2026, 12, client)
