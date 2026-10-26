"""The app-specific half of jeffco-sub-monitor's test/config.test.ts (16 cases).

The other half is library-owned and already covered elsewhere:
  - DISCORD_WEBHOOK_URL / STATUS_WEBHOOK_URL required + https-only + never-echoed
    -> tests/lib/test_config.py (env_required, env_https_url,
       load_runner_config's own tests).
  - POLL_INTERVAL_SEC / WINDOW_DAYS non-numeric, out-of-range, "allows zero
    jitter", "treats an empty value as absent" -> tests/lib/test_config.py
    (env_num's own tests). Below, one wiring test proves load_config actually
    reaches that validation rather than re-testing env_num itself, matching
    melanzana's test_the_library_validation_is_actually_wired_in.

What's left -- SFE_USER_ID/SFE_PIN (fields the library has never heard of), the
HS_SCHOOLS union semantics, the pinned timezone, and the 45s default -- is
app-specific and ported below.
"""

from dataclasses import replace

import pytest
from jeffco import config
from jeffco.alert import HS_GAP_FOOTER
from jeffco.config import DAD, LABELS, MOM, MOM_GAP_FOOTER, MOM_LABELS, load_config
from jeffco.schools import DEFAULT_HS_SCHOOLS, MOM_SCHOOLS, SchoolMatch, parse_school_list
from monitor.config import ConfigError

WEBHOOK = "https://discord.test/webhook"
BASE = {
    "JEFFCO_PROFILE": "dad",
    "SFE_USER_ID": "900001",
    "SFE_PIN": "secret",
    "DISCORD_WEBHOOK_URL": WEBHOOK,
}
MOM_ENV = {**BASE, "JEFFCO_PROFILE": "mom"}


def test_auto_accept_is_disabled_by_default() -> None:
    assert load_config(BASE).auto_accept_enabled is False


def test_auto_accept_can_be_explicitly_enabled() -> None:
    cfg = load_config({**BASE, "AUTO_ACCEPT_ENABLED": "true"})
    assert cfg.auto_accept_enabled is True


def test_auto_accept_uses_strict_shared_boolean_validation() -> None:
    cfg = load_config({**BASE, "AUTO_ACCEPT_ENABLED": "sometimes"})
    assert cfg.auto_accept_enabled is False


def test_applies_every_default_when_only_the_required_vars_are_provided() -> None:
    cfg = load_config(BASE)
    assert cfg.window_days == 180
    assert cfg.timezone == "America/Denver"
    assert cfg.runner.poll_interval_sec == 45
    assert cfg.runner.poll_jitter_pct == 20
    assert cfg.runner.state_path == "/data/state.json"
    assert cfg.runner.heartbeat_interval_sec == 86400
    assert cfg.runner.stall_alert_sec == 600
    assert cfg.runner.status_webhook_url is None


def test_parses_overrides_from_the_environment() -> None:
    cfg = load_config({**BASE, "POLL_INTERVAL_SEC": "30", "WINDOW_DAYS": "90"})
    assert cfg.runner.poll_interval_sec == 30
    assert cfg.window_days == 90


def test_the_library_validation_is_actually_wired_in() -> None:
    # One test that the primitives are reached, not a second copy of their own
    # suite -- env_num and env_https_url are covered in tests/lib/test_config.py.
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL is required"):
        load_config({"JEFFCO_PROFILE": "dad"})
    with pytest.raises(ConfigError, match="WINDOW_DAYS"):
        load_config({**BASE, "WINDOW_DAYS": "0"})


def test_the_pin_and_id_are_required() -> None:
    with pytest.raises(ConfigError, match="SFE_USER_ID is required"):
        load_config({"JEFFCO_PROFILE": "dad", "DISCORD_WEBHOOK_URL": WEBHOOK})
    with pytest.raises(ConfigError, match="SFE_PIN is required"):
        load_config(
            {"JEFFCO_PROFILE": "dad", "DISCORD_WEBHOOK_URL": WEBHOOK, "SFE_USER_ID": "900001"}
        )


def test_ships_the_built_in_school_list_normalized() -> None:
    cfg = load_config(BASE)
    assert len(cfg.schools) == len(DEFAULT_HS_SCHOOLS)
    assert "RALSTON VALLEY HS" in cfg.schools
    # "WHEAT RIDGE HIGH SCHOOL" is stored folded.
    assert "WHEAT RIDGE HS" in cfg.schools


def test_hs_schools_is_additive_never_replacing() -> None:
    # Additive, not replacing. The realistic edit is "a school got missed, let me
    # add it" -- and with replace semantics that one-line edit would silently drop
    # the other 21 campuses. Nothing legitimate wants a *smaller* list.
    #
    # Deviation from the brief: its illustrative snippet used "Doral Academy of
    # Colorado", which normalizes to a name already on DEFAULT_HS_SCHOOLS (see
    # jeffco.schools) and so asserts +1 where the real behaviour (correctly) adds
    # nothing. That case belongs to the double-count test below instead; this one
    # uses a genuinely new name, matching jeffco-sub-monitor/test/config.test.ts's
    # own "adds to the built-in list rather than replacing it".
    cfg = load_config({**BASE, "HS_SCHOOLS": "Somewhere New High School"})
    assert "SOMEWHERE NEW HS" in cfg.schools
    assert "RALSTON VALLEY HS" in cfg.schools
    assert len(cfg.schools) == len(DEFAULT_HS_SCHOOLS) + 1


def test_hs_schools_is_a_noop_when_empty() -> None:
    cfg = load_config({**BASE, "HS_SCHOOLS": ""})
    assert len(cfg.schools) == len(DEFAULT_HS_SCHOOLS)


def test_hs_schools_does_not_double_count_an_existing_school() -> None:
    cfg = load_config({**BASE, "HS_SCHOOLS": "Ralston Valley High School"})
    assert len(cfg.schools) == len(DEFAULT_HS_SCHOOLS)


def test_the_poll_interval_defaults_to_forty_five_not_ten() -> None:
    # Still slower than the official web client's own 30s refresh: the monitor
    # shares one login with the substitute it watches for, and a poll in flight
    # while he is on the site draws an HTTP 400 on one side or a stale listing on
    # the other. Do not lower this further while the login is shared.
    assert load_config(BASE).runner.poll_interval_sec == 45


def test_the_zone_is_not_read_from_the_environment() -> None:
    assert load_config({**BASE, "TIMEZONE": "Europe/Berlin"}).timezone == "America/Denver"


def test_the_ops_labels_are_jeffcos() -> None:
    # Both Jeffco instances post ops messages to one status channel, so each name
    # says whose monitor it is.
    assert LABELS.name == "Jeffco sub monitor (Dad)"
    assert LABELS.tracked_noun == "high school job(s)"
    assert LABELS.death_footer == "Liveness alert — check Available Jobs manually until it clears."
    assert load_config(BASE).runner.labels is LABELS


# --- profiles -----------------------------------------------------------------


def test_the_profile_is_required() -> None:
    # No default: a forgotten variable on Mom's instance would otherwise run Dad's
    # high-school filter against her feed while looking healthy.
    env = {k: v for k, v in BASE.items() if k != "JEFFCO_PROFILE"}
    with pytest.raises(ConfigError, match="JEFFCO_PROFILE is required"):
        load_config(env)


@pytest.mark.parametrize("raw", ["Mom", " mom", "dad ", "DAD", "both"])
def test_an_unknown_or_miscased_profile_is_refused(raw: str) -> None:
    with pytest.raises(ConfigError, match="JEFFCO_PROFILE must be one of: dad, mom"):
        load_config({**BASE, "JEFFCO_PROFILE": raw})


def test_dad_profile_keeps_the_high_school_filter_and_labels() -> None:
    cfg = load_config(BASE)
    assert cfg.profile is DAD
    assert cfg.profile.school_match is SchoolMatch.HIGH_SCHOOL
    assert cfg.profile.gap_footer == HS_GAP_FOOTER
    assert cfg.runner.labels is LABELS


def test_mom_profile_uses_her_named_list_and_labels() -> None:
    cfg = load_config(MOM_ENV)
    assert cfg.profile is MOM
    assert cfg.profile.school_match is SchoolMatch.NAMED
    assert cfg.schools == frozenset(parse_school_list("\n".join(MOM_SCHOOLS)))
    assert len(cfg.schools) == 19
    assert "RALSTON VALLEY HS" not in cfg.schools
    assert cfg.runner.labels is MOM_LABELS
    assert MOM_LABELS.name == "Jeffco sub monitor (Mom)"
    assert MOM_LABELS.tracked_noun == "job(s) at Mom's schools"
    assert MOM_LABELS.death_footer == LABELS.death_footer
    assert MOM_GAP_FOOTER == "If Mom should hear about any of these, add them to MOM_SCHOOLS."
    assert cfg.auto_accept_enabled is False
    assert cfg.runner.poll_interval_sec == 45


@pytest.mark.parametrize("raw", ["true", "TRUE", "True"])
def test_mom_profile_refuses_auto_accept(raw: str) -> None:
    with pytest.raises(
        ConfigError, match="AUTO_ACCEPT_ENABLED is not allowed for JEFFCO_PROFILE=mom"
    ):
        load_config({**MOM_ENV, "AUTO_ACCEPT_ENABLED": raw})


def test_mom_profile_accepts_an_explicit_false() -> None:
    assert load_config({**MOM_ENV, "AUTO_ACCEPT_ENABLED": "false"}).auto_accept_enabled is False


def test_mom_profile_refuses_hs_schools() -> None:
    with pytest.raises(ConfigError, match="HS_SCHOOLS is not allowed for JEFFCO_PROFILE=mom"):
        load_config({**MOM_ENV, "HS_SCHOOLS": "Somewhere New High School"})


@pytest.mark.parametrize("raw", ["", ";", " \n "])
def test_mom_profile_tolerates_a_blank_hs_schools(raw: str) -> None:
    assert len(load_config({**MOM_ENV, "HS_SCHOOLS": raw}).schools) == 19


def test_a_profile_with_no_schools_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(config.PROFILES, "mom", replace(MOM, built_in_schools=frozenset()))
    with pytest.raises(ConfigError, match="JEFFCO_PROFILE=mom has no schools"):
        load_config(MOM_ENV)
