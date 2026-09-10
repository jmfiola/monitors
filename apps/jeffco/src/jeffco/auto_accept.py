from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from jeffco.schools import normalize_school_name
from jeffco.types import Job

AUTO_ACCEPT_MIN_DAYS = 7
AUTO_ACCEPT_SCHOOLS = frozenset(
    normalize_school_name(name) for name in ("RALSTON VALLEY HS", "GOLDEN HS", "ARVADA WEST HS")
)


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


def is_auto_accept_eligible(job: Job, now_unix: int, timezone: str) -> bool:
    if job.duration_type != "FULL":
        return False
    if normalize_school_name(job.location_name) not in AUTO_ACCEPT_SCHOOLS:
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
