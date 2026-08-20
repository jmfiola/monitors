"""The Cowlendar availability endpoint. No auth of any kind: no token, no cookie,
no session — which is why melanzana never needed the abstraction jeffco does."""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlencode

import httpx

from melanzana.types import Slot

BASE = "https://app.cowlendar.com/extapi/calendar"

#: Browser-mimicking headers copied from the real widget request, which reduces
#: bot/block risk. Kept verbatim from the TypeScript client.
HEADERS: dict[str, str] = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://widget.cowlendar.com",
    "referer": "https://widget.cowlendar.com/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
}


class CowlendarError(Exception):
    """The availability request was rejected. A fault, not a busy signal — the
    runner backs off. Cowlendar has no equivalent of SmartFindExpress's 400."""


def build_availability_url(
    calendar_id: str, variant_id: str, timezone: str, year: int, month: int
) -> str:
    """One month's availability URL.

    `urlencode` matches `URLSearchParams` byte for byte on these inputs — both
    percent-encode the brackets and the slash in the zone identifier, both preserve
    insertion order. Verified against node during planning, and pinned in the test.
    """
    params = {
        "year": str(year),
        "month": str(month),
        "timezone": timezone,
        "quantity_details[0][type]": "default",
        "quantity_details[0][quantity]": "1",
        "quantity_details[0][name]": "Default",
        "teammate_id": "all",
        "duration": "30",
        "is_manual": "false",
        "is_pos": "false",
        "variant_id": variant_id,
    }
    return f"{BASE}/{calendar_id}/availability?{urlencode(params)}"


def _to_int(value: object) -> int | None:
    """A tolerant integer read. None when the value cannot be one.

    The TypeScript relies on `Number(x)` producing NaN and a later `> 0` filter
    dropping it. Python raises instead, so the coercion is explicit here and the
    caller skips the row — same outcome, visible reason.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def parse_availability(payload: object) -> list[Slot]:
    """Normalize the API payload into Slots. Tolerant of a missing or garbage `long`.

    A row without a usable key or start time is skipped rather than raised on: a
    malformed entry is one lost slot, whereas raising would stall the monitor and
    lose all of them.
    """
    if not isinstance(payload, dict):
        return []
    long: Any = payload.get("long")
    if not isinstance(long, list):
        return []

    slots: list[Slot] = []
    for row in long:
        if not isinstance(row, dict):
            continue
        raw_key = row.get("slot")
        key = "" if raw_key is None else str(raw_key)
        start_unix = _to_int(row.get("slot_start_unix"))
        if key == "" or start_unix is None or start_unix <= 0:
            continue
        qty_left = _to_int(row.get("qty_left"))
        slots.append(
            Slot(
                key=key,
                start_unix=start_unix,
                qty_left=0 if qty_left is None else qty_left,
                is_bookable=bool(row.get("is_bookable")),
            )
        )
    return slots


async def fetch_month(
    calendar_id: str,
    variant_id: str,
    timezone: str,
    year: int,
    month: int,
    client: httpx.AsyncClient,
) -> list[Slot]:
    """Fetch one month of availability. Raises `CowlendarError` on a non-2xx."""
    url = build_availability_url(calendar_id, variant_id, timezone, year, month)
    response = await client.get(url, headers=HEADERS)
    if not 200 <= response.status_code < 300:
        raise CowlendarError(f"Cowlendar request failed: HTTP {response.status_code}")
    return parse_availability(response.json())
