from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from monitor.types import HeartbeatExtras, Message, Monitor

from fashionjobs.alert import format_job_alert
from fashionjobs.site import FashionJobsError
from fashionjobs.types import FashionItem, FashionJob


class FashionJobsReader(Protocol):
    async def fetch(self) -> list[FashionItem]: ...


class FashionJobsMonitor:
    def __init__(self, source: FashionJobsReader) -> None:
        self._source = source

    async def fetch(self) -> list[FashionItem]:
        return await self._source.fetch()

    def key(self, item: FashionItem) -> str:
        return str(item.job_id)

    async def render(self, new: list[FashionItem]) -> list[Message]:
        jobs: list[FashionJob] = []
        for item in new:
            if not isinstance(item, FashionJob):
                raise FashionJobsError(f"new FashionJobs ID {item.job_id} has no job details")
            jobs.append(item)
        jobs.sort(key=lambda job: (job.published_at, job.job_id))
        return [format_job_alert(job) for job in jobs]

    def heartbeat_extras(self) -> HeartbeatExtras:
        return HeartbeatExtras()


if TYPE_CHECKING:  # a compile-time assertion, no runtime cost, no fake arguments

    def _assert_satisfies_protocol(
        monitor: FashionJobsMonitor,
    ) -> Monitor[FashionItem]:
        return monitor
