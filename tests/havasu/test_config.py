from datetime import date

import pytest
from havasu.config import LABELS, describe_ranges, load_config
from havasu.types import WatchRange
from monitor.config import ConfigError

WEBHOOK = "https://discord.test/webhook"


def test_defaults_are_the_production_watch_ranges_at_10s() -> None:
    cfg = load_config({"DISCORD_WEBHOOK_URL": WEBHOOK})
    assert cfg.watch_ranges == (
        WatchRange(date(2027, 5, 8), date(2027, 5, 31)),
        WatchRange(date(2027, 10, 1), date(2027, 10, 24)),
    )
    assert cfg.booking_url == "https://bookingsus.newbook.cloud/online/havasupai"
    assert cfg.runner.poll_interval_sec == 10
    assert cfg.runner.labels is LABELS


def test_all_watches_every_date() -> None:
    cfg = load_config({"DISCORD_WEBHOOK_URL": WEBHOOK, "WATCH_RANGES": "ALL"})
    assert cfg.watch_ranges is None
    assert describe_ranges(cfg.watch_ranges) == "all dates"


def test_ranges_are_sorted_and_described() -> None:
    cfg = load_config(
        {
            "DISCORD_WEBHOOK_URL": WEBHOOK,
            "WATCH_RANGES": "2027-10-01..2027-10-24, 2027-05-08..2027-05-08",
        }
    )
    assert describe_ranges(cfg.watch_ranges) == "2027-05-08..2027-05-08, 2027-10-01..2027-10-24"


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("2027-05-08", "must be YYYY-MM-DD"),
        ("2027-05-08..2027-05-32", "must be YYYY-MM-DD"),
        ("2027-05-08..2027-05-31,", "must be YYYY-MM-DD"),
        ("2027-05-31..2027-05-08", "ends before it starts"),
    ],
)
def test_bad_ranges_fail_at_startup(raw: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config({"DISCORD_WEBHOOK_URL": WEBHOOK, "WATCH_RANGES": raw})


def test_the_webhook_is_required() -> None:
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL is required"):
        load_config({})
