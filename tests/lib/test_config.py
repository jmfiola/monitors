import pytest
from monitor.config import (
    ConfigError,
    env_bool,
    env_https_url,
    env_num,
    env_required,
    env_str,
    env_time,
    load_runner_config,
    make_log,
)
from monitor.types import OpsLabels

LABELS = OpsLabels(name="X monitor", tracked_noun="thing(s)", death_footer="footer.")
WEBHOOK = "https://discord.test/webhook"


def test_env_num_falls_back_when_unset_or_empty() -> None:
    assert env_num({}, "N", 10) == 10
    assert env_num({"N": ""}, "N", 10) == 10


def test_env_num_parses_a_value() -> None:
    assert env_num({"N": "30"}, "N", 10) == 30


def test_env_num_rejects_a_non_number() -> None:
    with pytest.raises(ConfigError, match="N must be a number"):
        env_num({"N": "abc"}, "N", 10)


def test_env_num_rejects_a_non_finite_number() -> None:
    # float("inf") and float("nan") both parse where a JS Number() would too, and
    # both must be rejected: POLL_INTERVAL_SEC=inf is a monitor that never polls
    # again, and nan is worse — it compares false against every bound, so it slips
    # past the minimum check silently. The sign variants are here because a
    # string-matching implementation would catch "inf" and miss "-inf"/"nan".
    for raw in ("inf", "-inf", "infinity", "nan", "-nan"):
        with pytest.raises(ConfigError, match="N must be a number"):
            env_num({"N": raw}, "N", 10)


def test_env_num_rejects_a_value_below_its_minimum() -> None:
    with pytest.raises(ConfigError, match="N must be >= 1"):
        env_num({"N": "0"}, "N", 60)


def test_env_num_allows_zero_when_the_minimum_is_zero() -> None:
    assert env_num({"N": "0"}, "N", 20, minimum=0) == 0


def test_env_str_and_env_bool() -> None:
    assert env_str({}, "S", "fallback") == "fallback"
    assert env_str({"S": ""}, "S", "fallback") == "fallback"
    assert env_str({"S": "given"}, "S", "fallback") == "given"
    assert env_bool({}, "B", False) is False
    assert env_bool({"B": "true"}, "B", False) is True
    assert env_bool({"B": "TRUE"}, "B", False) is True
    assert env_bool({"B": "yes"}, "B", False) is False


def test_env_required_rejects_missing_and_empty() -> None:
    with pytest.raises(ConfigError, match="R is required"):
        env_required({}, "R")
    with pytest.raises(ConfigError, match="R is required"):
        env_required({"R": ""}, "R")
    assert env_required({"R": "v"}, "R") == "v"


def test_env_https_url_accepts_an_https_url() -> None:
    assert env_https_url("W", WEBHOOK) == WEBHOOK


def test_env_https_url_rejects_a_non_url_and_a_truncated_paste() -> None:
    # `https://` alone passes a startsWith check and then fails on the first POST,
    # hours later, in a place with no useful context.
    for raw in ("oops", "https://", "http://discord.test/webhook"):
        with pytest.raises(ConfigError, match="W must be an https:// URL"):
            env_https_url("W", raw)


def test_env_https_url_never_echoes_the_value() -> None:
    # A webhook's path IS its credential, and config errors get logged. Use a
    # rejected (non-https) URL so a raise actually happens, then check the
    # message: an accepted URL never reaches an error message at all.
    with pytest.raises(ConfigError) as exc:
        env_https_url("W", "http://discord.test/leaked-secret-path")
    assert "leaked-secret-path" not in str(exc.value)


def test_env_time_is_none_when_unset() -> None:
    assert env_time({}, "HEARTBEAT_AT") is None
    assert env_time({"HEARTBEAT_AT": ""}, "HEARTBEAT_AT") is None


def test_env_time_parses_hh_mm() -> None:
    assert env_time({"HEARTBEAT_AT": "07:00"}, "HEARTBEAT_AT") == (7, 0)
    assert env_time({"HEARTBEAT_AT": "00:00"}, "HEARTBEAT_AT") == (0, 0)
    assert env_time({"HEARTBEAT_AT": "23:59"}, "HEARTBEAT_AT") == (23, 59)


def test_env_time_raises_rather_than_ignoring_a_malformed_value() -> None:
    # Silently falling back to the interval would leave the operator believing a
    # 07:00 report is configured when it is not.
    for raw in ("7:00", "24:00", "07:60", "0700", "morning"):
        with pytest.raises(ConfigError, match="HEARTBEAT_AT must be an HH:MM"):
            env_time({"HEARTBEAT_AT": raw}, "HEARTBEAT_AT")


def test_make_log_stamps_every_line_with_the_app_name(capsys: pytest.CaptureFixture[str]) -> None:
    make_log("melanzana-monitor")("started")
    assert capsys.readouterr().out == "[melanzana-monitor] started\n"


def test_load_runner_config_applies_defaults() -> None:
    cfg = load_runner_config(
        {"DISCORD_WEBHOOK_URL": WEBHOOK},
        labels=LABELS,
        log_prefix="x-monitor",
        default_poll_interval_sec=10,
    )
    assert cfg.alert_webhook_url == WEBHOOK
    assert cfg.status_webhook_url is None
    assert cfg.state_path == "/data/state.json"
    assert cfg.poll_interval_sec == 10
    assert cfg.poll_jitter_pct == 20
    assert cfg.heartbeat_interval_sec == 86400
    assert cfg.heartbeat_at is None
    assert cfg.stall_alert_sec == 600
    assert cfg.max_backoff_sec == 300
    assert cfg.post_spacing_sec == 0.35
    assert cfg.labels is LABELS
    assert cfg.log_prefix == "x-monitor"


def test_load_runner_config_reads_every_shared_name() -> None:
    cfg = load_runner_config(
        {
            "DISCORD_WEBHOOK_URL": WEBHOOK,
            "STATUS_WEBHOOK_URL": "https://discord.test/ops",
            "STATE_PATH": "/tmp/state.json",
            "POLL_INTERVAL_SEC": "30",
            "POLL_JITTER_PCT": "0",
            "HEARTBEAT_INTERVAL_SEC": "3600",
            "HEARTBEAT_AT": "07:00",
            "STALL_ALERT_SEC": "120",
        },
        labels=LABELS,
        log_prefix="x-monitor",
        default_poll_interval_sec=10,
    )
    assert cfg.status_webhook_url == "https://discord.test/ops"
    assert cfg.state_path == "/tmp/state.json"
    assert cfg.poll_interval_sec == 30
    assert cfg.poll_jitter_pct == 0
    assert cfg.heartbeat_interval_sec == 3600
    assert cfg.heartbeat_at == (7, 0)
    assert cfg.stall_alert_sec == 120


def test_load_runner_config_requires_an_https_alert_webhook() -> None:
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL is required"):
        load_runner_config({}, labels=LABELS, log_prefix="x", default_poll_interval_sec=10)
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL must be an https"):
        load_runner_config(
            {"DISCORD_WEBHOOK_URL": "oops"},
            labels=LABELS,
            log_prefix="x",
            default_poll_interval_sec=10,
        )


def test_load_runner_config_validates_the_status_webhook_too() -> None:
    with pytest.raises(ConfigError, match="STATUS_WEBHOOK_URL must be an https"):
        load_runner_config(
            {"DISCORD_WEBHOOK_URL": WEBHOOK, "STATUS_WEBHOOK_URL": "oops"},
            labels=LABELS,
            log_prefix="x",
            default_poll_interval_sec=10,
        )


def test_status_url_falls_back_to_the_alert_webhook() -> None:
    base = {"DISCORD_WEBHOOK_URL": WEBHOOK}
    cfg = load_runner_config(base, labels=LABELS, log_prefix="x", default_poll_interval_sec=10)
    assert cfg.status_url == WEBHOOK

    separate = load_runner_config(
        {**base, "STATUS_WEBHOOK_URL": "https://discord.test/ops"},
        labels=LABELS,
        log_prefix="x",
        default_poll_interval_sec=10,
    )
    assert separate.status_url == "https://discord.test/ops"


def test_busy_stall_defaults_to_an_hour_and_is_not_read_from_the_environment() -> None:
    # Deliberately not wired to an env var yet: a variable nothing consumes is
    # config that silently does nothing. Same reasoning that withheld HEARTBEAT_AT.
    cfg = load_runner_config(
        {"DISCORD_WEBHOOK_URL": WEBHOOK, "BUSY_STALL_ALERT_SEC": "7"},
        labels=LABELS,
        log_prefix="x",
        default_poll_interval_sec=10,
    )
    assert cfg.busy_stall_alert_sec == 3600
