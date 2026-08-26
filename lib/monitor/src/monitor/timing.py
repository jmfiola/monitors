"""Poll-cadence arithmetic.

A wrong credential retried on the poll cadence is ~1,920 attempts a day. If the
account belongs to a real person, that can cost them access to the thing the
monitor exists to watch. Any authenticated source needs a failure ceiling, not
just backoff — and this module deliberately does not provide one, because only
jeffco has authentication and hoisting its lockout guard here would be a shared
abstraction with exactly one consumer.
"""

from __future__ import annotations

import time
from collections.abc import Callable

# Five minutes. A 30-minute cap would silently undo the poll cadence for half an
# hour after a single transient blip.
MAX_BACKOFF_SEC: float = 300


def system_now() -> int:
    """The wall clock, in epoch seconds.

    One definition, shared by the runner and by every app that needs the time, so
    `main.py` can hand the same callable to both and a test that freezes it freezes
    everything.
    """
    return int(time.time())


def with_jitter(base_sec: float, jitter_pct: float, rand: Callable[[], float]) -> float:
    """Apply +/- jitter to a base interval.

    `jitter_pct` is a percentage (e.g. 20 = +/-20%). `rand()` must return a value
    in [0,1). The result is clamped to >= 1 second.
    """
    offset = base_sec * (jitter_pct / 100) * (rand() * 2 - 1)
    return max(1.0, base_sec + offset)


def next_backoff(current_sec: float, base_sec: float, max_sec: float) -> float:
    """Exponential backoff. Doubles `current_sec`, capped at `max_sec`.

    When `current_sec` is 0 (no prior backoff), starts at `base_sec`.
    """
    nxt = base_sec if current_sec <= 0 else current_sec * 2
    return min(nxt, max_sec)
