"""Havasu's item types. The library never inspects them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

#: Every campground permit is 4 days / 3 nights. Not configurable: the site sells
#: no other length.
NIGHTS = 3

#: Explicit tables, not `strftime`: `%a`/`%b` follow the host locale (invariant 13).
#: Indexed by `date.weekday()` (Monday=0) and `month - 1`.
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True, order=True)
class YearMonth:
    year: int
    month: int  # 1-12

    @classmethod
    def of(cls, day: date) -> YearMonth:
        return cls(day.year, day.month)

    def next(self) -> YearMonth:
        return (
            YearMonth(self.year + 1, 1)
            if self.month == 12
            else YearMonth(self.year, self.month + 1)
        )

    def through(self, last: YearMonth) -> list[YearMonth]:
        """Every month from this one to `last`, inclusive."""
        out: list[YearMonth] = []
        month = self
        while month <= last:
            out.append(month)
            month = month.next()
        return out

    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True)
class WatchRange:
    """Inclusive bounds on the ARRIVAL date only. The stay may end after `end`."""

    start: date
    end: date


@dataclass(frozen=True)
class DayState:
    """One calendar cell, as NewBook classed it."""

    #: `available` — at least one campground permit is left for that night.
    available: bool
    #: `closed_arrival` — no arrivals that day. Seen on 2026-12-01, the first
    #: closed day after the season's last open night.
    closed_arrival: bool = False
    #: `closed_departure` — no departures that day.
    closed_departure: bool = False


@dataclass(frozen=True)
class OpenArrival:
    """An arrival date on which a 3-night campground permit can be booked now."""

    arrival: date

    @property
    def departure(self) -> date:
        return self.arrival + timedelta(days=NIGHTS)
