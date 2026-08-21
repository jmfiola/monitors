from datetime import datetime

import pytest
from fashionjobs.monitor import FashionJobsMonitor
from fashionjobs.site import FashionJobsError
from fashionjobs.types import FashionItem, FashionJob, KnownJob
from monitor.types import HeartbeatExtras


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
