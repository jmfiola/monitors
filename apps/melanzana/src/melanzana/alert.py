"""The day-card embed. Alert formatting is per-app by design: melanzana renders one
embed with a field per day, jeffco renders one message per job."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from monitor.types import GREEN, Embed, Field, Payload

from melanzana.types import Slot

#: Discord allows 25 embed fields; one is reserved for the overflow note.
MAX_DAY_CARDS = 24

WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_KEY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2})")


def _parse_key(key: str) -> tuple[str, str, str]:
    """Split a slot key ("YYYY-MM-DD HH:MM") into (day bucket, day header, time).

    Display only — the original slot key stays the diff identity. A key that does
    not start with a date falls back to the raw key, with no grouping magic.

    The TypeScript anchors its `Date` at noon UTC so the weekday cannot cross a day
    boundary; a Python `date` has no time component, so no anchor is needed and the
    weekday is the same. `isoweekday() % 7` converts Python's Monday=1 to
    JavaScript's `getUTCDay()` Sunday=0.
    """
    match = _KEY.match(key)
    if match is None:
        return key, f"📅 {key}", key
    year, month, day, time = match.groups()
    try:
        weekday = WEEKDAYS[date(int(year), int(month), int(day)).isoweekday() % 7]
    except ValueError:
        # The regex matches the SHAPE of a date, not a real one — "2026-02-30" gets
        # this far. Falling back to the raw-key branch rather than propagating,
        # because a raise here reaches run_tick as "these items were not announced",
        # which withholds every fresh key and retries the same failure every tick
        # forever. One malformed slot string must not be able to silence all alerting.
        return key, f"📅 {key}", key
    return (
        f"{year}-{month}-{day}",
        f"📅 {weekday}, {MONTHS[int(month) - 1]} {int(day)}",
        time,
    )


def format_alert(slots: Sequence[Slot], *, booking_url: str, mention_everyone: bool) -> Payload:
    """One embed for a batch of newly-available slots, rendered as an inline day
    card per day (time — spots left) in chronological order."""
    by_day: dict[str, tuple[str, list[str]]] = {}
    for s in slots:
        day_key, header, time = _parse_key(s.key)
        _, lines = by_day.setdefault(day_key, (header, []))
        lines.append(f"{time} — {s.qty_left} left")

    # Sorting the day keys: every key is either a same-shape ISO date or a raw
    # fallback, and for those `localeCompare` and code-point order agree. Sorting
    # the lines works for the same reason — zero-padded HH:MM sorts chronologically.
    days = sorted(by_day.items(), key=lambda item: item[0])

    fields = [
        Field(name=header, value="\n".join(sorted(lines)), inline=True)
        for _, (header, lines) in days[:MAX_DAY_CARDS]
    ]
    if len(days) > MAX_DAY_CARDS:
        fields.append(
            Field(
                name="…",
                value=(f"and {len(days) - MAX_DAY_CARDS} more day(s) — tap the title to see all."),
                inline=False,
            )
        )

    payload = Payload(
        embeds=(
            Embed(
                title="🟢 New Melanzana appointment(s) available!",
                url=booking_url,
                description=f"{len(slots)} open slot(s) across {len(days)} day(s):",
                color=GREEN,
                fields=tuple(fields),
                footer_text="Tap the title to book — slots go fast.",
            ),
        )
    )
    if mention_everyone:
        payload = replace(payload, content="@everyone", allowed_mentions_parse=("everyone",))
    return payload
