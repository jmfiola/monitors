"""The arrival-date embed: one message for every newly opened arrival date."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from monitor.types import GREEN, Embed, Field, Payload

from havasu.types import MONTHS, WEEKDAYS, OpenArrival

#: Discord allows 25 embed fields; one is reserved for the overflow note. Sale day
#: opens every watched date at once, so the overflow path is a real one.
MAX_DATE_CARDS = 24

FOOTER = "One permit per person — add each with “Add Booking”, then check out. Spots go fast."


def _day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {MONTHS[d.month - 1]} {d.day}"


def format_alert(arrivals: Sequence[OpenArrival], *, booking_url: str) -> Payload:
    """One embed, one inline card per arrival date in chronological order."""
    ordered = sorted(arrivals, key=lambda a: a.arrival)
    fields = [
        Field(name=f"📅 {_day(a.arrival)}, {a.arrival.year}", value=f"out {_day(a.departure)}")
        for a in ordered[:MAX_DATE_CARDS]
    ]
    hidden = len(ordered) - MAX_DATE_CARDS
    if hidden > 0:
        fields.append(
            Field(
                name="…",
                value=f"and {hidden} more date(s) — tap the title to see all.",
                inline=False,
            )
        )
    return Payload(
        embeds=(
            Embed(
                title="🟢 Havasu campground permit(s) open!",
                url=booking_url,
                description=f"{len(ordered)} arrival date(s) open for a 3-night stay:",
                color=GREEN,
                fields=tuple(fields),
                footer_text=FOOTER,
            ),
        )
    )
