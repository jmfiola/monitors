"""Liveness bookkeeping: the state the loop folds each tick, and the predicates
that decide when the monitor should speak about itself.

The health *server* is deliberately not here. Production runs with `health=off`,
which made `startHealthServer`, `toSnapshot`, and `HealthSnapshot` dead code in
the TypeScript app along with the assertions covering them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

# Pinned rather than configurable, for the same reason jeffco pins it in code:
# making the zone an env var only creates a way to typo an identifier and raise on
# every tick forever. It is the operator's clock, not the watched site's, so it is
# right even for a source in another country.
OPERATOR_TZ = ZoneInfo("America/Denver")


@dataclass(frozen=True)
class HealthState:
    """In-memory liveness snapshot, folded once per tick by the loop.

    `items_tracked` is the generic name for what the TypeScript apps called
    `slotsTracked` and `jobsTracked`. The count is shared; the noun it is read
    with is not, which is why `OpsLabels.tracked_noun` exists.
    """

    started_unix: int
    last_success_unix: int
    consecutive_failures: int
    items_tracked: int
    last_heartbeat_unix: int
    death_alerted: bool
    #: True when every failure since the last success was a busy signal — see
    #: should_alert_stall for what that buys and why it is not an exemption.
    busy_only: bool = False


def init_health(started_unix: int) -> HealthState:
    """Fresh state at process start.

    Seeds `last_success_unix` AND `last_heartbeat_unix` to `started_unix`, so boot
    neither looks stalled nor emits an immediate heartbeat.
    """
    return HealthState(
        started_unix=started_unix,
        last_success_unix=started_unix,
        consecutive_failures=0,
        items_tracked=0,
        last_heartbeat_unix=started_unix,
        death_alerted=False,
        busy_only=False,
    )


def should_alert_stall(state: HealthState, now: int, stall_sec: int, busy_stall_sec: int) -> bool:
    """True when the absence of data has gone on long enough to say something.

    A shared-login collision is not a fault — the upstream is reachable and the
    cause is the account holder using their own account — so ten minutes of it must
    not page anyone. But it is still an absence of data, so it cannot be exempt: an
    upstream that answers "busy" *permanently* would otherwise produce indefinite
    silence underneath a heartbeat still reporting "still watching", which is the
    failure this project trades everything else against.

    So a busy-only absence gets an hour, and the moment any other fault occurs
    `busy_only` is forfeited and the short threshold governs again.
    """
    threshold = busy_stall_sec if state.busy_only else stall_sec
    # >= : the boundary instant itself counts as stalled.
    return now - state.last_success_unix >= threshold


def should_heartbeat(
    state: HealthState,
    now: int,
    interval_sec: int,
    heartbeat_at: tuple[int, int] | None = None,
) -> bool:
    """True when a heartbeat is due.

    **`heartbeat_at` unset** — `interval_sec` since the last heartbeat. This is
    what melanzana does today, and what keeps its port at parity by default.

    **`heartbeat_at` set**, as `(hour, minute)` in `OPERATOR_TZ` — due when the
    local date has advanced since the last heartbeat *and* the local time is at or
    past it. Both conditions are needed: the date check is what stops it firing
    repeatedly for the rest of the day.

    Why the hour exists at all: `interval_sec` alone anchors the heartbeat to
    process start, so the hour it arrives is whatever time the last deploy
    happened, it re-anchors on every restart, and it creeps later by up to one poll
    interval a day. An ops message that turns up at 01:05 because that is when a
    migration finished is not useful.
    """
    if heartbeat_at is None:
        # >= : the boundary instant itself counts as due.
        return now - state.last_heartbeat_unix >= interval_sec

    local_now = datetime.fromtimestamp(now, OPERATOR_TZ)
    local_last = datetime.fromtimestamp(state.last_heartbeat_unix, OPERATOR_TZ)
    # <= rather than == : "the date changed" means advanced. A clock stepped
    # backwards by an NTP correction is not a new day.
    if local_now.date() <= local_last.date():
        return False
    return (local_now.hour, local_now.minute) >= heartbeat_at
