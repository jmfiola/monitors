import pytest
from melanzana.config import LABELS, load_config
from monitor.config import ConfigError

WEBHOOK = "https://discord.test/webhook"


def test_applies_every_default_when_only_the_webhook_is_provided() -> None:
    cfg = load_config({"DISCORD_WEBHOOK_URL": WEBHOOK})
    assert cfg.calendar_id == "685b42f202405a8372cd6b78"
    assert cfg.variant_id == "41855678382123"
    assert cfg.window_days == 60
    assert cfg.timezone == "America/Denver"
    assert cfg.booking_url == "https://melanzana.com/pages/how-to-shop"
    assert cfg.mention_everyone is False
    assert cfg.runner.poll_interval_sec == 10
    assert cfg.runner.poll_jitter_pct == 20
    assert cfg.runner.state_path == "/data/state.json"
    assert cfg.runner.heartbeat_interval_sec == 86400
    assert cfg.runner.stall_alert_sec == 600
    # Unset by default, which is what keeps the port at parity: the heartbeat
    # behaves exactly as it does today until Terraform sets the hour.
    assert cfg.runner.heartbeat_at is None
    assert cfg.runner.status_webhook_url is None


def test_parses_overrides_from_the_environment() -> None:
    cfg = load_config(
        {
            "DISCORD_WEBHOOK_URL": WEBHOOK,
            "POLL_INTERVAL_SEC": "30",
            "WINDOW_DAYS": "90",
            "MENTION_EVERYONE": "true",
            "HEARTBEAT_AT": "07:00",
        }
    )
    assert cfg.runner.poll_interval_sec == 30
    assert cfg.window_days == 90
    assert cfg.mention_everyone is True
    assert cfg.runner.heartbeat_at == (7, 0)


def test_the_library_validation_is_actually_wired_in() -> None:
    # One test that the primitives are reached, not a second copy of their own
    # suite — env_num and env_https_url are covered in tests/lib/test_config.py.
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL is required"):
        load_config({})
    with pytest.raises(ConfigError, match="WINDOW_DAYS"):
        load_config({"DISCORD_WEBHOOK_URL": WEBHOOK, "WINDOW_DAYS": "0"})


def test_the_ops_labels_are_melanzanas() -> None:
    # These exact strings are parity-critical: the differential harness renders the
    # heartbeat and both status alerts from them, so a typo here surfaces as a diff
    # pointing at the library rather than at this line.
    assert LABELS.name == "Melanzana monitor"
    assert LABELS.tracked_noun == "slot(s)"
    assert LABELS.death_footer == "Liveness alert — the monitor may be blocked or down."
    assert load_config({"DISCORD_WEBHOOK_URL": WEBHOOK}).runner.labels is LABELS
