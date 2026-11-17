"""Which months to ask for, and which arrival dates the answer opens.

Pure functions, no I/O. The rule was checked against NewBook's own per-arrival
chart on 2026-11-16: Nov 16 and Nov 28 booked, Nov 29 and Nov 30 did not because
Dec 1 was closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from havasu.types import NIGHTS, DayState, OpenArrival, WatchRange, YearMonth


def query_window(ranges: Sequence[WatchRange] | None, today: date) -> tuple[YearMonth, YearMonth]:
    """The first and last month one calendar request must cover.

    The last month reaches the latest departure day, not just the latest arrival:
    the arrival rule reads nights D+1 and D+2 and the departure day D+3, and a month
    that was never fetched would read as "not available" rather than as unknown.

    `None` means every date, for testing: this month through December of next year.
    """
    if ranges is None:
        return YearMonth.of(today), YearMonth(today.year + 1, 12)
    first = min(r.start for r in ranges)
    last = max(r.end for r in ranges) + timedelta(days=NIGHTS)
    return YearMonth.of(first), YearMonth.of(last)


def months_between(first: YearMonth, last: YearMonth) -> list[YearMonth]:
    out: list[YearMonth] = []
    month = first
    while month <= last:
        out.append(month)
        month = month.next()
    return out


def is_open_arrival(days: Mapping[date, DayState], arrival: date) -> bool:
    """Nights D, D+1, D+2 all available, arrivals allowed on D, departures on D+3.

    Departure-day *availability* does not matter — Nov 28 2026 booked with Dec 1
    closed — but a `closed_departure` on D+3 would make the stay impossible.
    """
    nights = [days.get(arrival + timedelta(days=i)) for i in range(NIGHTS)]
    if any(night is None or not night.available for night in nights):
        return False
    if nights[0] is not None and nights[0].closed_arrival:
        return False
    departure = days.get(arrival + timedelta(days=NIGHTS))
    return not (departure is not None and departure.closed_departure)


def in_ranges(arrival: date, ranges: Sequence[WatchRange] | None) -> bool:
    return ranges is None or any(r.start <= arrival <= r.end for r in ranges)


def open_arrivals(
    days: Mapping[date, DayState], ranges: Sequence[WatchRange] | None, today: date
) -> list[OpenArrival]:
    """Every watched arrival from today on that can be booked, chronologically.

    Past days come back `booked` anyway; the `today` cut keeps a stray past cell
    from ever producing an alert nobody can act on.
    """
    return [
        OpenArrival(day)
        for day in sorted(days)
        if day >= today and in_ranges(day, ranges) and is_open_arrival(days, day)
    ]
