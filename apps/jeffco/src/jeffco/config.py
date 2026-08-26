"""Jeffco's schema. The library owns the shared names; these are the ones only
this app reads."""

from __future__ import annotations

from dataclasses import dataclass

from monitor.config import Env, RunnerConfig, env_num, env_required, env_str, load_runner_config
from monitor.types import OpsLabels

from jeffco.schools import DEFAULT_HS_SCHOOLS, parse_school_list

#: Written once. It is the log prefix, and `main.py` reads it from here rather than
#: repeating the string in a print().
LOG_PREFIX = "jeffco-sub-monitor"

LABELS = OpsLabels(
    name="Jeffco sub monitor",
    tracked_noun="high school job(s)",
    death_footer="Liveness alert — check Available Jobs manually until it clears.",
)

#: Normalized once at import time -- the raw list never changes at runtime, so
#: re-normalizing it on every `load_config` call would be pure waste.
_DEFAULT_HS_SCHOOLS_NORMALIZED = parse_school_list("\n".join(DEFAULT_HS_SCHOOLS))


@dataclass(frozen=True)
class JeffcoConfig:
    runner: RunnerConfig
    sfe_user_id: str
    sfe_pin: str
    #: The built-in list UNION whatever HS_SCHOOLS adds -- see load_config. Never
    #: the configured value alone: there is no way to shrink this from the
    #: environment.
    hs_schools: frozenset[str]
    window_days: int
    #: Not read from the environment -- see load_config.
    timezone: str


def load_config(env: Env) -> JeffcoConfig:
    """Build a validated config from the environment. Raises on anything unusable."""
    return JeffcoConfig(
        runner=load_runner_config(
            env,
            labels=LABELS,
            log_prefix=LOG_PREFIX,
            # 45s. Still far slower than melanzana's 10s: this monitor logs in as
            # the substitute it watches for, and a poll in flight while he is on
            # the site draws an HTTP 400 on one side or a stale listing on the
            # other (see jeffco.sfe.is_account_busy). The extra 15s of notice is
            # paid for in collisions. Do NOT lower this further toward melanzana
            # while the login is shared with a person.
            default_poll_interval_sec=45,
        ),
        sfe_user_id=env_required(env, "SFE_USER_ID"),
        sfe_pin=env_required(env, "SFE_PIN"),
        # Additive, never replacing: built-in list UNION whatever HS_SCHOOLS adds.
        # The realistic edit is "a school got missed, let me add it" -- replace
        # semantics would turn that one-liner into a silent loss of the other 21
        # campuses, invisible until a job at one of them went unalerted.
        hs_schools=frozenset(
            _DEFAULT_HS_SCHOOLS_NORMALIZED | parse_school_list(env_str(env, "HS_SCHOOLS", ""))
        ),
        window_days=int(env_num(env, "WINDOW_DAYS", 180)),
        # Not from the environment: the district and the API both operate in
        # Denver time, so a TIMEZONE typo here would become a silent
        # ZoneInfoNotFoundError on every tick rather than a config error at
        # startup that names the actual problem.
        timezone="America/Denver",
    )
