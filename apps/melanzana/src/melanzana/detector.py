"""Which months to ask for, and which of the returned slots count.

Every arithmetic operation here is UTC, matching the TypeScript's `getUTCMonth`
and `getUTCFullYear`. Melanzana has no local-time or DST logic anywhere, which is
what makes it the low-risk first port.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from melanzana.types import Slot

DAY_SEC = 86400
#: Two days on both ends. The pads absorb the gap between UTC month enumeration and
#: the API's local (Denver) date grouping: e.g. early on the 1st UTC it is still the
#: prior day locally, so a near-term slot can sit in the previous month's query.
#: Over-fetching a boundary month is harmless — filter_bookable does the precise cut.
PAD_DAYS = 2


@dataclass(frozen=True)
class MonthKey:
    year: int
    month: int  # 1-12


def months_to_fetch(now_unix: int, window_days: int) -> list[MonthKey]:
    """Every calendar month (UTC) the window spans, padded on both ends."""
    start = datetime.fromtimestamp(now_unix - PAD_DAYS * DAY_SEC, UTC)
    end = datetime.fromtimestamp(now_unix + (window_days + PAD_DAYS) * DAY_SEC, UTC)

    out: list[MonthKey] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append(MonthKey(year, month))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return out


def filter_bookable(slots: Sequence[Slot], now_unix: int, window_days: int) -> list[Slot]:
    """Slots that are bookable, have spots left, and start within [now, now + window]."""
    window_end = now_unix + window_days * DAY_SEC
    return [
        s
        for s in slots
        if s.is_bookable and s.qty_left > 0 and now_unix <= s.start_unix <= window_end
    ]
