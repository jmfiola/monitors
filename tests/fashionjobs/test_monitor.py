from dataclasses import replace
from datetime import datetime

import pytest
from fashionjobs.config import LABELS, load_config
from fashionjobs.monitor import FashionJobsMonitor
from fashionjobs.site import FashionJobsError
from fashionjobs.types import FashionItem, FashionJob, KnownJob
from monitor.config import RunnerConfig
from monitor.discord import format_heartbeat
from monitor.health import init_health
from monitor.runner import run_tick
from monitor.types import HeartbeatExtras, Payload


def runner_config() -> RunnerConfig:
    return load_config({"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/test"}).runner


async def no_status(_payload: Payload) -> None:
    return None


async def no_sleep(_seconds: float) -> None:
    return None


def _job(
    *,
    job_id: int = 12000001,
    title: str = "Stage Assistant Produit",
    published_at: datetime | None = None,
) -> FashionJob:
    return FashionJob(
        job_id=job_id,
        title=title,
        company="MAISON EXEMPLE",
        location="Paris",
        contract="Stage",
        published_at=published_at or datetime.fromisoformat("2026-08-21T21:50:27+02:00"),
        url=f"https://fr.fashionjobs.com/emploi/Stage-assistant-produit,{job_id}.html",
    )


class _FakeSource:
    def __init__(self, items: list[FashionItem]) -> None:
        self.items = items

    async def fetch(self) -> list[FashionItem]:
        return list(self.items)


async def test_first_run_baselines_all_jobs_without_alerting() -> None:
    jobs: list[FashionItem] = [_job(job_id=1), _job(job_id=2)]
    monitor = FashionJobsMonitor(_FakeSource(jobs))
    posted: list[Payload] = []

    async def poster(_url: str, payload: Payload) -> None:
        posted.append(payload)

    keys = await run_tick(
        monitor,
        runner_config(),
        set(),
        is_first_run=True,
        poster=poster,
        post_status=no_status,
        sleep=no_sleep,
        log=lambda _message: None,
    )

    assert keys == {"1", "2"}
    assert posted == []


async def test_restart_with_persisted_ids_does_not_duplicate_alerts() -> None:
    monitor = FashionJobsMonitor(_FakeSource([_job(job_id=1), _job(job_id=2)]))
    posted: list[Payload] = []

    async def poster(_url: str, payload: Payload) -> None:
        posted.append(payload)

    keys = await run_tick(
        monitor,
        runner_config(),
        {"1", "2"},
        is_first_run=False,
        poster=poster,
        post_status=no_status,
        sleep=no_sleep,
        log=lambda _message: None,
    )

    assert keys == {"1", "2"}
    assert posted == []


async def test_failed_notification_is_withheld_then_retried_successfully() -> None:
    job = _job(job_id=7)
    monitor = FashionJobsMonitor(_FakeSource([job]))

    async def failing_poster(_url: str, _payload: Payload) -> None:
        raise RuntimeError("Discord down")

    first_keys = await run_tick(
        monitor,
        runner_config(),
        set(),
        is_first_run=False,
        poster=failing_poster,
        post_status=no_status,
        sleep=no_sleep,
        log=lambda _message: None,
    )

    assert first_keys == set()

    posted: list[Payload] = []

    async def successful_poster(_url: str, payload: Payload) -> None:
        posted.append(payload)

    second_keys = await run_tick(
        monitor,
        runner_config(),
        first_keys,
        is_first_run=False,
        poster=successful_poster,
        post_status=no_status,
        sleep=no_sleep,
        log=lambda _message: None,
    )

    assert second_keys == {"7"}
    assert len(posted) == 1


async def test_multiple_new_jobs_are_posted_in_deterministic_order() -> None:
    jobs: list[FashionItem] = [
        _job(
            job_id=3,
            title="Job 3",
            published_at=datetime.fromisoformat("2026-08-21T11:00:00+02:00"),
        ),
        _job(
            job_id=2,
            title="Job 2",
            published_at=datetime.fromisoformat("2026-08-21T10:00:00+02:00"),
        ),
        _job(
            job_id=1,
            title="Job 1",
            published_at=datetime.fromisoformat("2026-08-21T10:00:00+02:00"),
        ),
    ]
    monitor = FashionJobsMonitor(_FakeSource(jobs))
    posted_titles: list[str] = []

    async def poster(_url: str, payload: Payload) -> None:
        posted_titles.append(payload.embeds[0].title)

    keys = await run_tick(
        monitor,
        runner_config(),
        set(),
        is_first_run=False,
        poster=poster,
        post_status=no_status,
        sleep=no_sleep,
        log=lambda _message: None,
    )

    assert keys == {"1", "2", "3"}
    assert posted_titles == ["Job 1", "Job 2", "Job 3"]


def test_heartbeat_reports_the_tracked_listing_identity_count() -> None:
    state = replace(init_health(1000), items_tracked=4)

    payload = format_heartbeat(LABELS, state, 4600)

    assert "tracking 4 listing identity(ies)" in payload.embeds[0].description


async def test_render_orders_new_jobs_by_timestamp_then_numeric_id() -> None:
    newer = _job(
        job_id=3,
        published_at=datetime.fromisoformat("2026-08-21T11:00:00+02:00"),
    )
    same_time_high = _job(
        job_id=2,
        published_at=datetime.fromisoformat("2026-08-21T10:00:00+02:00"),
    )
    same_time_low = _job(
        job_id=1,
        published_at=datetime.fromisoformat("2026-08-21T10:00:00+02:00"),
    )
    monitor = FashionJobsMonitor(_FakeSource([newer, same_time_high, same_time_low]))

    messages = await monitor.render([newer, same_time_high, same_time_low])

    assert [message.covers for message in messages] == [("1",), ("2",), ("3",)]


def test_key_uses_only_the_numeric_job_id() -> None:
    monitor = FashionJobsMonitor(_FakeSource([]))
    assert monitor.key(_job(job_id=12000001, title="edited")) == "12000001"
    assert monitor.key(KnownJob(job_id=12000001)) == "12000001"


async def test_a_fresh_placeholder_fails_render_instead_of_banking_an_empty_alert() -> None:
    monitor = FashionJobsMonitor(_FakeSource([]))
    with pytest.raises(FashionJobsError, match=r"12000001.*no job details"):
        await monitor.render([KnownJob(job_id=12000001)])


def test_heartbeat_extras_are_empty() -> None:
    monitor = FashionJobsMonitor(_FakeSource([]))
    assert monitor.heartbeat_extras() == HeartbeatExtras()
