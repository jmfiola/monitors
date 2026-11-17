"""Havasu's schema. The library owns the shared names; these are the ones only
this app reads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from monitor.config import ConfigError, Env, RunnerConfig, env_str, load_runner_config
from monitor.types import OpsLabels

from havasu.newbook import BOOKING_PAGE
from havasu.types import WatchRange

LOG_PREFIX = "havasu-monitor"

LABELS = OpsLabels(
    name="Havasu monitor",
    tracked_noun="open arrival date(s)",
    death_footer=(
        "Liveness alert — the monitor may be blocked or down. "
        "Check the booking page by hand until it recovers."
    ),
)

#: Jacob's 2027 arrival windows. A range bounds the arrival date only.
DEFAULT_WATCH_RANGES = "2027-05-08..2027-05-31,2027-10-01..2027-10-24"
#: Every date the calendar returns. For testing, not production.
ALL_DATES = "all"


@dataclass(frozen=True)
class HavasuConfig:
    runner: RunnerConfig
    #: `None` means every date (`WATCH_RANGES=all`).
    watch_ranges: tuple[WatchRange, ...] | None
    booking_url: str


def parse_watch_ranges(raw: str) -> tuple[WatchRange, ...] | None:
    """`all`, or comma-separated inclusive `YYYY-MM-DD..YYYY-MM-DD` arrival ranges."""
    if raw.strip().lower() == ALL_DATES:
        return None
    ranges: list[WatchRange] = []
    for part in raw.split(","):
        bounds = part.strip().split("..")
        try:
            if len(bounds) != 2:
                raise ValueError
            start, end = date.fromisoformat(bounds[0]), date.fromisoformat(bounds[1])
        except ValueError:
            raise ConfigError(
                f'Config error: WATCH_RANGES entries must be YYYY-MM-DD..YYYY-MM-DD, got "{part}"'
            ) from None
        if end < start:
            raise ConfigError(f'Config error: WATCH_RANGES range ends before it starts: "{part}"')
        ranges.append(WatchRange(start, end))
    return tuple(sorted(ranges, key=lambda r: r.start))


def describe_ranges(ranges: tuple[WatchRange, ...] | None) -> str:
    if ranges is None:
        return "all dates"
    return ", ".join(f"{r.start.isoformat()}..{r.end.isoformat()}" for r in ranges)


def load_config(env: Env) -> HavasuConfig:
    """Build a validated config from the environment. Raises on anything unusable."""
    return HavasuConfig(
        runner=load_runner_config(
            env,
            labels=LABELS,
            log_prefix=LOG_PREFIX,
            # 10s. One anonymous calendar request per tick, no account to collide
            # with — and a released permit is gone in minutes.
            default_poll_interval_sec=10,
        ),
        watch_ranges=parse_watch_ranges(env_str(env, "WATCH_RANGES", DEFAULT_WATCH_RANGES)),
        booking_url=env_str(env, "BOOKING_URL", BOOKING_PAGE),
    )
