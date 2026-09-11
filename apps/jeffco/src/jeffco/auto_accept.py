from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from jeffco.schools import normalize_school_name
from jeffco.sfe import SfeHtmlResponseError, SfeHttpError, SfeProtocolError
from jeffco.types import Job

AUTO_ACCEPT_MIN_DAYS = 7
AUTO_ACCEPT_SCHOOLS = frozenset(
    normalize_school_name(name)
    for name in ("RALSTON VALLEY HS", "GOLDEN HS", "ARVADA WEST HS", "STANDLEY LAKE HS")
)
AUTO_ACCEPT_SUBJECT_TERMS = (
    "MATH",
    "ALGEBRA",
    "GEOMETRY",
    "CALCULUS",
    "STATISTICS",
    "TRIGONOMETRY",
    "SCIENCE",
    "BIOLOGY",
    "CHEMISTRY",
    "PHYSICS",
    "ASTRONOMY",
    "GEOLOGY",
    "ECOLOGY",
)
RETRY_DELAYS = (1.0, 3.0)

type Sleeper = Callable[[float], Awaitable[None]]


class AcceptanceClient(Protocol):
    async def accept_job(self, job_id: int) -> None: ...

    async def has_job_assignment(self, job_id: int) -> bool: ...


class AutoAcceptStatus(StrEnum):
    ACCEPTED = "accepted"
    FAILED = "failed"


@dataclass(frozen=True)
class AutoAcceptOutcome:
    status: AutoAcceptStatus
    detail: str | None = None


@dataclass(frozen=True)
class AutoAcceptItem:
    job: Job
    outcome: AutoAcceptOutcome


type JeffcoItem = Job | AutoAcceptItem


def _is_auto_accept_subject(subject: str | None) -> bool:
    if subject is None:
        return False
    normalized = subject.upper()
    return any(term in normalized for term in AUTO_ACCEPT_SUBJECT_TERMS)


def is_auto_accept_eligible(job: Job, now_unix: int, timezone: str) -> bool:
    if job.duration_type != "FULL":
        return False
    if normalize_school_name(job.location_name) not in AUTO_ACCEPT_SCHOOLS:
        return False
    if not _is_auto_accept_subject(job.classf_name):
        return False
    try:
        start = datetime.fromisoformat(job.job_start.replace("Z", "+00:00"))
    except ValueError:
        return False
    if start.tzinfo is None or start.utcoffset() is None:
        return False
    zone = ZoneInfo(timezone)
    today = datetime.fromtimestamp(now_unix, zone).date()
    return start.astimezone(zone).date() >= today + timedelta(days=AUTO_ACCEPT_MIN_DAYS)


def auto_accept_key(item: AutoAcceptItem) -> str:
    return f"auto-{item.outcome.status.value}:{item.job.job_id}"


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, (SfeHtmlResponseError, SfeHttpError)):
        return error.status in {408, 429} or 500 <= error.status <= 599
    if isinstance(error, SfeProtocolError):
        return True
    return isinstance(error, httpx.TransportError)


def _failure_detail(error: Exception) -> str:
    if isinstance(error, (SfeHtmlResponseError, SfeHttpError)):
        return f"HTTP {error.status}"
    if isinstance(error, SfeProtocolError):
        return "transport error"
    if isinstance(error, httpx.TimeoutException):
        return "request timed out"
    if isinstance(error, httpx.TransportError):
        return "transport error"
    return type(error).__name__


class AutoAccepter:
    def __init__(
        self,
        client: AcceptanceClient,
        *,
        timezone: str,
        now_unix: Callable[[], int],
        sleep: Sleeper,
        log: Callable[[str], None],
    ) -> None:
        self._client = client
        self._timezone = timezone
        self._now_unix = now_unix
        self._sleep = sleep
        self._log = log
        self._decisions: dict[int, AutoAcceptItem] = {}

    async def reconcile(self, jobs: Sequence[Job]) -> list[JeffcoItem]:
        now = self._now_unix()
        visible = {candidate.job_id for candidate in jobs}
        self._decisions = {
            job_id: decision for job_id, decision in self._decisions.items() if job_id in visible
        }
        decided_before_tick = frozenset(self._decisions)
        outcome_ids: set[int] = set()
        reconciled: list[JeffcoItem] = []
        for candidate in jobs:
            job_id = candidate.job_id
            eligible = is_auto_accept_eligible(candidate, now, self._timezone)

            # A decision from an earlier tick owns this identity until it
            # disappears. Emitting the cached wrapper keeps its outcome key in
            # runner state even if today's row is temporarily ineligible.
            if job_id in decided_before_tick:
                if job_id not in outcome_ids:
                    reconciled.append(self._decisions[job_id])
                    outcome_ids.add(job_id)
                elif not eligible:
                    # Row eligibility still governs same-tick duplicate rows.
                    reconciled.append(candidate)
                continue

            if not eligible:
                reconciled.append(candidate)
                continue
            decision = self._decisions.get(job_id)
            if decision is None:
                decision = await self._attempt(candidate)
                self._decisions[job_id] = decision
            if job_id in outcome_ids:
                continue
            reconciled.append(decision)
            outcome_ids.add(job_id)
        return reconciled

    async def _attempt(self, job: Job) -> AutoAcceptItem:
        for attempt in range(3):
            if attempt > 0:
                await self._sleep(RETRY_DELAYS[attempt - 1])
            try:
                await self._client.accept_job(job.job_id)
            except Exception as error:
                detail = _failure_detail(error)
                try:
                    assigned = await self._client.has_job_assignment(job.job_id)
                except Exception as verification_error:
                    self._log(
                        f"job assignment verification {job.job_id} failed "
                        f"({_failure_detail(verification_error)})"
                    )
                else:
                    if assigned:
                        self._log(
                            f"job {job.job_id} is assigned after an ambiguous acceptance result"
                        )
                        return AutoAcceptItem(
                            job,
                            AutoAcceptOutcome(AutoAcceptStatus.ACCEPTED),
                        )
                if not _is_retryable(error) or attempt == 2:
                    self._log(f"job {job.job_id} auto-accept failed ({detail})")
                    return AutoAcceptItem(
                        job,
                        AutoAcceptOutcome(AutoAcceptStatus.FAILED, detail),
                    )
                continue
            self._log(f"job {job.job_id} automatically accepted")
            return AutoAcceptItem(
                job,
                AutoAcceptOutcome(AutoAcceptStatus.ACCEPTED),
            )
        raise AssertionError("acceptance attempt loop did not return")
