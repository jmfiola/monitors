"""Jeffco's schema. The library owns the shared names; these are the ones only
this app reads."""

from __future__ import annotations

from dataclasses import dataclass

from monitor.config import (
    ConfigError,
    Env,
    RunnerConfig,
    env_bool,
    env_num,
    env_required,
    env_str,
    load_runner_config,
)
from monitor.types import OpsLabels

from jeffco.schools import DEFAULT_HS_SCHOOLS, parse_school_list

#: Written once. It is the log prefix, and `main.py` reads it from here rather than
#: repeating the string in a print().
LOG_PREFIX = "jeffco-sub-monitor"

#: Both Jeffco instances post ops messages to one status channel, so each name
#: says whose monitor it is.
LABELS = OpsLabels(
    name="Jeffco sub monitor (Dad)",
    tracked_noun="high school job(s)",
    death_footer="Liveness alert — check Available Jobs manually until it clears.",
)

MOM_LABELS = OpsLabels(
    name="Jeffco sub monitor (Mom)",
    tracked_noun="job(s) in Mom's feed",
    death_footer=LABELS.death_footer,
)

#: Normalized once at import time -- the raw list never changes at runtime, so
#: re-normalizing it on every `load_config` call would be pure waste.
_DEFAULT_HS_SCHOOLS_NORMALIZED = parse_school_list("\n".join(DEFAULT_HS_SCHOOLS))


@dataclass(frozen=True)
class Profile:
    """Whose jobs one instance watches for. Defined in code, selected per instance
    by JEFFCO_PROFILE, so the product rules stay testable and no deployment typo
    can reshape them."""

    name: str
    labels: OpsLabels
    #: Normalized, matched with the high-school pattern. None means no school
    #: filter, which is never the same as an empty list -- see load_config.
    built_in_schools: frozenset[str] | None
    allows_auto_accept: bool


DAD = Profile(
    name="dad",
    labels=LABELS,
    built_in_schools=frozenset(_DEFAULT_HS_SCHOOLS_NORMALIZED),
    allows_auto_accept=True,
)

#: No school filter: her SFE account settings decide which jobs reach her feed.
MOM = Profile(
    name="mom",
    labels=MOM_LABELS,
    built_in_schools=None,
    allows_auto_accept=False,
)

PROFILES: dict[str, Profile] = {profile.name: profile for profile in (DAD, MOM)}


@dataclass(frozen=True)
class JeffcoConfig:
    runner: RunnerConfig
    sfe_user_id: str
    sfe_pin: str
    profile: Profile
    auto_accept_enabled: bool
    #: The profile's built-in list UNION whatever HS_SCHOOLS adds -- see
    #: load_config. Never the configured value alone: there is no way to shrink
    #: this from the environment. None when the profile has no school filter.
    schools: frozenset[str] | None
    window_days: int
    #: Not read from the environment -- see load_config.
    timezone: str


def _load_profile(env: Env) -> Profile:
    # Required, with no default: a forgotten variable on Mom's instance would
    # otherwise run Dad's high-school filter against her feed while every signal
    # read healthy. The raw value is not echoed, matching the library's errors.
    profile = PROFILES.get(env_required(env, "JEFFCO_PROFILE"))
    if profile is None:
        raise ConfigError(
            f"Config error: JEFFCO_PROFILE must be one of: {', '.join(sorted(PROFILES))}"
        )
    return profile


def load_config(env: Env) -> JeffcoConfig:
    """Build a validated config from the environment. Raises on anything unusable."""
    profile = _load_profile(env)
    runner = load_runner_config(
        env,
        labels=profile.labels,
        log_prefix=LOG_PREFIX,
        # 45s. Still far slower than melanzana's 10s: this monitor logs in as
        # the substitute it watches for, and a poll in flight while he is on
        # the site draws an HTTP 400 on one side or a stale listing on the
        # other (see jeffco.sfe.is_account_busy). The extra 15s of notice is
        # paid for in collisions. Do NOT lower this further toward melanzana
        # while the login is shared with a person.
        default_poll_interval_sec=45,
    )
    sfe_user_id = env_required(env, "SFE_USER_ID")
    sfe_pin = env_required(env, "SFE_PIN")

    # Refused rather than ignored: a setting that silently does nothing invites
    # someone to believe it works.
    auto_accept_enabled = env_bool(env, "AUTO_ACCEPT_ENABLED", False)
    if auto_accept_enabled and not profile.allows_auto_accept:
        raise ConfigError(
            f"Config error: AUTO_ACCEPT_ENABLED is not allowed for JEFFCO_PROFILE={profile.name}"
        )
    extra_schools = parse_school_list(env_str(env, "HS_SCHOOLS", ""))
    if extra_schools and profile.built_in_schools is None:
        raise ConfigError(
            f"Config error: HS_SCHOOLS is not allowed for JEFFCO_PROFILE={profile.name}"
        )

    # Additive, never replacing: built-in list UNION whatever HS_SCHOOLS adds.
    # The realistic edit is "a school got missed, let me add it" -- replace
    # semantics would turn that one-liner into a silent loss of the other 21
    # campuses, invisible until a job at one of them went unalerted.
    schools = (
        None
        if profile.built_in_schools is None
        else frozenset(profile.built_in_schools | extra_schools)
    )
    # An empty list is not "every job" (that is None): it keeps only what the
    # pattern happens to match, while the heartbeat reads healthy.
    if schools is not None and not schools:
        raise ConfigError(f"Config error: JEFFCO_PROFILE={profile.name} has no schools")

    return JeffcoConfig(
        runner=runner,
        sfe_user_id=sfe_user_id,
        sfe_pin=sfe_pin,
        profile=profile,
        auto_accept_enabled=auto_accept_enabled,
        schools=schools,
        window_days=int(env_num(env, "WINDOW_DAYS", 180)),
        # Not from the environment: the district and the API both operate in
        # Denver time, so a TIMEZONE typo here would become a silent
        # ZoneInfoNotFoundError on every tick rather than a config error at
        # startup that names the actual problem.
        timezone="America/Denver",
    )
