"""Jeffco's item types. The library never inspects them."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    """One available job, narrowed to the fields the monitor reads.

    Only the first four are validated at parse time. The rest are optional by
    construction, not just by convention, because SFE may omit any of them — and
    every consumer already guards.
    """

    job_id: int
    location_name: str
    job_start: str  # ISO-8601 UTC, no seconds: "2026-10-16T13:45Z"
    job_end: str
    classf_name: str | None = None
    employee_first_name: str | None = None
    employee_last_name: str | None = None
    days_of_week: str | None = None
    duration_type: str | None = None
    job_status: str | None = None


@dataclass(frozen=True)
class JobDay:
    """One worked day, resolved from GET /api/job/{id}. Epoch seconds."""

    start_unix: int
    end_unix: int
