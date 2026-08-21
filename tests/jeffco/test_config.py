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
HS_SCHOOLS union semantics, the pinned timezone, and the 60s default -- is
app-specific and ported below.
"""

import pytest
from jeffco.config import LABELS, load_config
from jeffco.schools import DEFAULT_HS_SCHOOLS
from monitor.config import ConfigError

WEBHOOK = "https://discord.test/webhook"
BASE = {"SFE_USER_ID": "900001", "SFE_PIN": "secret", "DISCORD_WEBHOOK_URL": WEBHOOK}


def test_applies_every_default_when_only_the_required_vars_are_provided() -> None:
    cfg = load_config(BASE)
    assert cfg.window_days == 180
    assert cfg.timezone == "America/Denver"
    assert cfg.runner.poll_interval_sec == 60
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
        load_config({})
    with pytest.raises(ConfigError, match="WINDOW_DAYS"):
        load_config({**BASE, "WINDOW_DAYS": "0"})


def test_the_pin_and_id_are_required() -> None:
    with pytest.raises(ConfigError, match="SFE_USER_ID is required"):
        load_config({"DISCORD_WEBHOOK_URL": WEBHOOK})
    with pytest.raises(ConfigError, match="SFE_PIN is required"):
        load_config({"DISCORD_WEBHOOK_URL": WEBHOOK, "SFE_USER_ID": "900001"})


def test_ships_the_built_in_school_list_normalized() -> None:
    cfg = load_config(BASE)
    assert len(cfg.hs_schools) == len(DEFAULT_HS_SCHOOLS)
    assert "RALSTON VALLEY HS" in cfg.hs_schools
    # "WHEAT RIDGE HIGH SCHOOL" is stored folded.
    assert "WHEAT RIDGE HS" in cfg.hs_schools


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
    assert "SOMEWHERE NEW HS" in cfg.hs_schools
    assert "RALSTON VALLEY HS" in cfg.hs_schools
    assert len(cfg.hs_schools) == len(DEFAULT_HS_SCHOOLS) + 1


def test_hs_schools_is_a_noop_when_empty() -> None:
    cfg = load_config({**BASE, "HS_SCHOOLS": ""})
    assert len(cfg.hs_schools) == len(DEFAULT_HS_SCHOOLS)


def test_hs_schools_does_not_double_count_an_existing_school() -> None:
    cfg = load_config({**BASE, "HS_SCHOOLS": "Ralston Valley High School"})
    assert len(cfg.hs_schools) == len(DEFAULT_HS_SCHOOLS)


def test_the_poll_interval_defaults_to_sixty_not_ten() -> None:
    # Deliberately slower than the official web client's own 30s refresh: the
    # monitor shares one login with the substitute it watches for, and a poll in
    # flight while he is on the site draws an HTTP 400 on one side or a stale
    # listing on the other. Do not lower this while the login is shared.
    assert load_config(BASE).runner.poll_interval_sec == 60


def test_the_zone_is_not_read_from_the_environment() -> None:
    assert load_config({**BASE, "TIMEZONE": "Europe/Berlin"}).timezone == "America/Denver"


def test_the_ops_labels_are_jeffcos() -> None:
    # These exact strings are parity-critical: the differential harness renders the
    # heartbeat and both status alerts from them, so a typo here surfaces as a
    # diff pointing at the library rather than at this line.
    assert LABELS.name == "Jeffco sub monitor"
    assert LABELS.tracked_noun == "high school job(s)"
    assert LABELS.death_footer == "Liveness alert — check Available Jobs manually until it clears."
    assert load_config(BASE).runner.labels is LABELS
