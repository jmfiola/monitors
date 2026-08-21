"""The four contract methods. Everything the library does not own lives here."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from monitor.timing import system_now
from monitor.types import HeartbeatExtras, Message, Monitor, SourceBusy

from jeffco.alert import format_job_alerts, heartbeat_extras_for
from jeffco.config import JeffcoConfig
from jeffco.dates import format_approximate, format_job_days, parse_job_days
from jeffco.schools import partition_jobs
from jeffco.sfe import SfeClient, is_account_busy
from jeffco.types import Job


class JeffcoMonitor:
    """Implements `monitor.runner.Monitor[Job]`.

    Holds the accumulated filter-gap names for the process lifetime -- see
    `_unmatched_seen`. Melanzana holds no state between ticks; jeffco holds this
    plus the token cache and lockout counters `SfeClient` already owns on its own.
    """

    def __init__(
        self,
        cfg: JeffcoConfig,
        sfe: SfeClient,
        log: Callable[[str], None],
        now_unix: Callable[[], int] = system_now,
    ) -> None:
        self._cfg = cfg
        self._sfe = sfe
        self._log = log
        # `now_unix` is not read anywhere below -- every SFE call already carries
        # its own clock via `SfeClient`. Held anyway so `main.py` can pass the SAME
        # callable here and to `run_forever`, per the Monitor protocol's clock
        # convention: an app that later needs "now" inside this class gets it for
        # free, rather than a second, possibly-different reading being threaded in.
        self._now_unix = now_unix
        # A `dict[str, None]` rather than a `set[str]`: a dict keeps insertion
        # order, and `heartbeat_extras_for`'s "newest, not first" truncation (see
        # its docstring in alert.py) only does the right thing if the names it is
        # given arrive oldest-first. A plain set has no order at all, and sorting
        # alphabetically would let names starting with 'A' permanently occupy the
        # visible slots -- the exact failure "newest, not first" exists to
        # prevent, just triggered by spelling instead of by arrival time.
        self._unmatched_seen: dict[str, None] = {}

    async def fetch(self) -> list[Job]:
        """Every currently-available High School job.

        `is_account_busy` re-raises as `SourceBusy` so the runner holds its normal
        cadence instead of escalating backoff -- see jeffco.sfe.is_account_busy for
        why an SFE 400 here means "the account holder is on the site", not a fault.

        Only HS keys ever enter state: `partition_jobs` splits the raw list here
        and only the HS half is returned, so a non-HS job's id can never be banked
        as "seen" and cannot be resurrected as "new" by a later edit to the school
        list. The unmatched half is absorbed into `_unmatched_seen` for the
        heartbeat's gap report, logging each name only the first time it is seen.
        """
        try:
            jobs = await self._sfe.fetch_available_jobs()
        except Exception as err:
            if is_account_busy(err):
                raise SourceBusy(str(err)) from err
            raise

        hs, unmatched = partition_jobs(jobs, self._cfg.hs_schools)
        for name in unmatched:
            if name in self._unmatched_seen:
                continue
            self._unmatched_seen[name] = None
            self._log(f'filter gap: "{name}" matched neither the school list nor the pattern')
        return hs

    def key(self, item: Job) -> str:
        """The job id alone -- deliberately not a content hash.

        An edited job (a changed subject, a shifted end time) must not re-alert:
        the ask is "a new position was posted", not "this posting changed". And a
        job that is claimed and then released must re-alert, because that is a
        genuine new opportunity. Keying on the id alone gives both for free.
        """
        return str(item.job_id)

    async def render(self, new: list[Job]) -> list[Message]:
        """One message per job, each carrying its own resolved date line.

        A failed detail fetch degrades only the one job it belongs to -- it falls
        back to `format_approximate` and the loop continues -- rather than raising
        and losing the whole batch. That matters more here than it did in the
        TypeScript: under this library, a `render()` raise withholds *every* fresh
        key in the batch (see monitor.runner.run_tick), so one unreachable detail
        endpoint must not silence an entire tick's alerts.
        """
        pairs: list[tuple[Job, str]] = []
        for job in new:
            pairs.append((job, await self._resolve_date_line(job)))
        return format_job_alerts(pairs)

    async def _resolve_date_line(self, job: Job) -> str:
        try:
            days = parse_job_days(await self._sfe.fetch_job_detail(job.job_id))
        except Exception as err:
            self._log(f"job detail {job.job_id} fetch failed ({err}); using approximate dates")
            return format_approximate(job, self._cfg.timezone)
        if days:
            return format_job_days(days, self._cfg.timezone)
        self._log(f"job detail {job.job_id} had no usable days; falling back to approximate dates")
        return format_approximate(job, self._cfg.timezone)

    def heartbeat_extras(self) -> HeartbeatExtras:
        """The filter-gap report, oldest-seen first -- see `_unmatched_seen`."""
        return heartbeat_extras_for(list(self._unmatched_seen))


if TYPE_CHECKING:  # a compile-time assertion, no runtime cost, no fake arguments

    def _assert_satisfies_protocol(m: JeffcoMonitor) -> Monitor[Job]:
        return m
