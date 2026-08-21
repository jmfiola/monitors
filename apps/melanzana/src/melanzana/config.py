"""Melanzana's schema. The library owns the shared names; these are the ones only
this app reads."""

from __future__ import annotations

from dataclasses import dataclass

from monitor.config import Env, RunnerConfig, env_bool, env_num, env_str, load_runner_config
from monitor.types import OpsLabels

#: Written once. It is the log prefix, and `main.py` reads it from here rather than
#: repeating the string in a print().
LOG_PREFIX = "melanzana-monitor"

LABELS = OpsLabels(
    name="Melanzana monitor",
    tracked_noun="slot(s)",
    death_footer="Liveness alert — the monitor may be blocked or down.",
)


@dataclass(frozen=True)
class MelanzanaConfig:
    runner: RunnerConfig
    calendar_id: str
    variant_id: str
    window_days: int
    #: The zone Cowlendar groups its slots by — a *request parameter*, not the
    #: operator's clock. HEARTBEAT_AT is interpreted in monitor.health.OPERATOR_TZ
    #: and is deliberately unrelated to this.
    timezone: str
    booking_url: str
    mention_everyone: bool


def load_config(env: Env) -> MelanzanaConfig:
    """Build a validated config from the environment. Raises on anything unusable."""
    return MelanzanaConfig(
        runner=load_runner_config(
            env,
            labels=LABELS,
            log_prefix=LOG_PREFIX,
            # 10s. Cowlendar needs no auth and has no session to collide with, so
            # unlike jeffco there is no reason to poll slower.
            default_poll_interval_sec=10,
        ),
        calendar_id=env_str(env, "CALENDAR_ID", "685b42f202405a8372cd6b78"),
        variant_id=env_str(env, "VARIANT_ID", "41855678382123"),
        window_days=int(env_num(env, "WINDOW_DAYS", 60)),
        timezone=env_str(env, "TIMEZONE", "America/Denver"),
        booking_url=env_str(env, "BOOKING_URL", "https://melanzana.com/pages/how-to-shop"),
        mention_everyone=env_bool(env, "MENTION_EVERYONE", False),
    )
