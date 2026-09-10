from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from jeffco.auto_accept import (
    AutoAcceptItem,
    AutoAcceptOutcome,
    AutoAcceptStatus,
    auto_accept_key,
    is_auto_accept_eligible,
)
from jeffco.types import Job

DENVER = "America/Denver"
NOW = int(datetime(2026, 9, 10, 12, tzinfo=ZoneInfo(DENVER)).timestamp())


def job(
    *,
    location_name: str = "RALSTON VALLEY HS",
    job_start: str = "2026-09-17T13:45:00Z",
    duration_type: str | None = "FULL",
    job_id: int = 101,
) -> Job:
    return Job(
        job_id=job_id,
        location_name=location_name,
        job_start=job_start,
        job_end=job_start,
        duration_type=duration_type,
    )


@pytest.mark.parametrize(
    "school",
    [
        "RALSTON VALLEY HS",
        "Ralston Valley High School",
        "GOLDEN HIGH SCHOOL",
        "Golden Sr High",
        "ARVADA WEST HS",
        "Arvada West High School",
    ],
)
def test_only_the_three_normalized_target_schools_qualify(school: str) -> None:
    assert is_auto_accept_eligible(job(location_name=school), NOW, DENVER)


@pytest.mark.parametrize(
    "school",
    ["WHEAT RIDGE HIGH SCHOOL", "ARVADA HIGH SCHOOL", "LAKEWOOD HIGH SCHOOL"],
)
def test_other_high_schools_do_not_qualify(school: str) -> None:
    assert not is_auto_accept_eligible(job(location_name=school), NOW, DENVER)


def test_exactly_seven_denver_calendar_days_qualifies() -> None:
    assert is_auto_accept_eligible(job(job_start="2026-09-17T06:00:00Z"), NOW, DENVER)


def test_six_denver_calendar_days_does_not_qualify() -> None:
    assert not is_auto_accept_eligible(job(job_start="2026-09-16T13:45:00Z"), NOW, DENVER)


@pytest.mark.parametrize("duration", [None, "", "HALF_DAY_AM", "full", "FULL DAY"])
def test_only_the_exact_full_duration_qualifies(duration: str | None) -> None:
    assert not is_auto_accept_eligible(job(duration_type=duration), NOW, DENVER)


@pytest.mark.parametrize("start", ["not-a-date", "2026-09-17T07:45:00", ""])
def test_bad_or_naive_start_timestamps_fail_closed(start: str) -> None:
    assert not is_auto_accept_eligible(job(job_start=start), NOW, DENVER)


def test_lead_time_uses_denver_dates_across_fall_back() -> None:
    now = int(datetime(2026, 10, 26, 23, 30, tzinfo=ZoneInfo(DENVER)).timestamp())
    assert is_auto_accept_eligible(
        job(job_start="2026-11-02T07:00:00Z"),
        now,
        DENVER,
    )


def test_outcome_keys_cannot_collide_with_an_existing_job_id() -> None:
    accepted = AutoAcceptItem(
        job(),
        AutoAcceptOutcome(AutoAcceptStatus.ACCEPTED),
    )
    failed = AutoAcceptItem(
        job(),
        AutoAcceptOutcome(AutoAcceptStatus.FAILED, "HTTP 400"),
    )
    assert auto_accept_key(accepted) == "auto-accepted:101"
    assert auto_accept_key(failed) == "auto-failed:101"
    assert auto_accept_key(accepted) != str(accepted.job.job_id)
