"""Environment primitives, and the schema the *runner* acts on.

Every app's own schema is its own — melanzana reads CALENDAR_ID, jeffco reads
SFE_PIN, and neither belongs here. What is shared is the parsing, the validation,
and the handful of names the loop itself consumes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from monitor.timing import MAX_BACKOFF_SEC
from monitor.types import OpsLabels

Env = Mapping[str, str | None]


class ConfigError(ValueError):
    """A configuration value is missing or unusable. Raised at startup, never later."""


def _raw(env: Env, key: str) -> str | None:
    """The value, treating empty string as unset — an env file line `FOO=` is a
    variable someone commented out by deleting its value, not a value of ""."""
    value = env.get(key)
    return None if value is None or value == "" else value


def env_num(env: Env, key: str, fallback: float, minimum: float = 1) -> float:
    raw = _raw(env, key)
    if raw is None:
        return fallback
    try:
        parsed = float(raw)
    except ValueError:
        raise ConfigError(f'Config error: {key} must be a number, got "{raw}"') from None
    # isfinite: float("inf") and float("nan") both parse. POLL_INTERVAL_SEC=inf is
    # a monitor that never polls again, and nan compares false against every bound.
    if not math.isfinite(parsed):
        raise ConfigError(f'Config error: {key} must be a number, got "{raw}"')
    if parsed < minimum:
        raise ConfigError(f'Config error: {key} must be >= {minimum}, got "{raw}"')
    return parsed


def env_str(env: Env, key: str, fallback: str) -> str:
    raw = _raw(env, key)
    return fallback if raw is None else raw


def env_bool(env: Env, key: str, fallback: bool) -> bool:
    """Only the exact string "true" (any case) is true.

    Deliberately not a list of truthy spellings: MENTION_EVERYONE decides whether a
    channel of people gets pinged, and "yes" quietly meaning false is a smaller
    failure than a permissive parser meaning true by accident.
    """
    raw = _raw(env, key)
    return fallback if raw is None else raw.lower() == "true"


def env_required(env: Env, key: str) -> str:
    raw = _raw(env, key)
    if raw is None:
        raise ConfigError(f"Config error: {key} is required")
    return raw


def env_https_url(key: str, raw: str) -> str:
    """Validate a webhook URL. Two rules, both load-bearing.

    1. Parsed, not prefix-matched. A truncated paste like `https://` passes a
       `startswith` check and then fails on the first POST — hours later, in a
       place with no useful context.
    2. The message never contains `raw`. A Discord webhook's path IS its
       credential, and config errors get logged.
    """
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigError(f"Config error: {key} must be an https:// URL")
    return raw


_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def env_time(env: Env, key: str) -> tuple[int, int] | None:
    """Parse an `HH:MM` 24-hour local time. None when unset.

    Raises rather than ignoring a malformed value: this variable exists to make the
    heartbeat hour predictable, so silently falling back to the interval would
    leave the operator believing a 07:00 report is configured when it is not. An
    HH:MM is not a credential, so echoing it is safe and useful.
    """
    raw = _raw(env, key)
    if raw is None:
        return None
    match = _HHMM.match(raw)
    if match is None:
        raise ConfigError(f'Config error: {key} must be an HH:MM 24-hour local time, got "{raw}"')
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True)
class RunnerConfig:
    """Everything `run_forever` reads. An app holds this plus its own fields."""

    alert_webhook_url: str
    status_webhook_url: str | None
    state_path: str
    poll_interval_sec: float
    poll_jitter_pct: float
    heartbeat_interval_sec: int
    heartbeat_at: tuple[int, int] | None
    stall_alert_sec: int
    labels: OpsLabels
    log_prefix: str
    max_backoff_sec: float = MAX_BACKOFF_SEC
    # Gap between the messages of one batch. Discord allows roughly five requests
    # per two seconds per webhook — so 0.35s leaves almost no margin, and a 429 is
    # handled explicitly in run_tick rather than merely made unlikely here. A code
    # constant, not an env var: it needs a reason attached, not a knob.
    post_spacing_sec: float = 0.35

    @property
    def status_url(self) -> str:
        """Where ops messages go: the separate status channel when one is
        configured, else the main alert channel. Both are credentials — the point of
        naming this is that neither is ever logged."""
        return self.status_webhook_url or self.alert_webhook_url


def make_log(prefix: str) -> Callable[[str], None]:
    """A logger that stamps every line with the app's name.

    `flush=True` because the container's stdout is what `docker logs` and the Cloud
    Logging agent read, and a crashed process must not lose its last words to a
    buffer.
    """

    def log(message: str) -> None:
        print(f"[{prefix}] {message}", flush=True)

    return log


def load_runner_config(
    env: Env,
    *,
    labels: OpsLabels,
    log_prefix: str,
    default_poll_interval_sec: float,
    default_state_path: str = "/data/state.json",
) -> RunnerConfig:
    """Read the shared names. The poll interval's default is per-app: melanzana
    polls every 10s, jeffco every 60s because it shares a login with a person."""
    status_raw = _raw(env, "STATUS_WEBHOOK_URL")
    return RunnerConfig(
        alert_webhook_url=env_https_url(
            "DISCORD_WEBHOOK_URL", env_required(env, "DISCORD_WEBHOOK_URL")
        ),
        status_webhook_url=(
            None if status_raw is None else env_https_url("STATUS_WEBHOOK_URL", status_raw)
        ),
        state_path=env_str(env, "STATE_PATH", default_state_path),
        poll_interval_sec=env_num(env, "POLL_INTERVAL_SEC", default_poll_interval_sec),
        poll_jitter_pct=env_num(env, "POLL_JITTER_PCT", 20, minimum=0),
        heartbeat_interval_sec=int(env_num(env, "HEARTBEAT_INTERVAL_SEC", 86400)),
        heartbeat_at=env_time(env, "HEARTBEAT_AT"),
        stall_alert_sec=int(env_num(env, "STALL_ALERT_SEC", 600)),
        labels=labels,
        log_prefix=log_prefix,
    )
