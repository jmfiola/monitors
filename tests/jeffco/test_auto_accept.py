from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from jeffco.auto_accept import (
    AutoAccepter,
    AutoAcceptItem,
    AutoAcceptOutcome,
    AutoAcceptStatus,
    auto_accept_key,
    is_auto_accept_eligible,
)
from jeffco.sfe import SfeHttpError
from jeffco.types import Job

DENVER = "America/Denver"
NOW = int(datetime(2026, 9, 10, 12, tzinfo=ZoneInfo(DENVER)).timestamp())


class FakeAcceptanceClient:
    def __init__(
        self,
        accepts: Sequence[Exception | None],
        assignments: Sequence[Exception | bool] = (),
    ) -> None:
        self.accepts = list(accepts)
        self.assignments = list(assignments)
        self.accepted_ids: list[int] = []
        self.verified_ids: list[int] = []

    async def accept_job(self, job_id: int) -> None:
        self.accepted_ids.append(job_id)
        result = self.accepts.pop(0)
        if isinstance(result, Exception):
            raise result

    async def has_job_assignment(self, job_id: int) -> bool:
        self.verified_ids.append(job_id)
        result = self.assignments.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def job(
    *,
    location_name: str = "RALSTON VALLEY HS",
    job_start: str = "2026-09-17T13:45:00Z",
    duration_type: str | None = "FULL",
    classf_name: str | None = "SEC MATH",
    job_id: int = 101,
) -> Job:
    return Job(
        job_id=job_id,
        location_name=location_name,
        job_start=job_start,
        job_end=job_start,
        classf_name=classf_name,
        duration_type=duration_type,
    )


async def _record_sleep(values: list[float], seconds: float) -> None:
    values.append(seconds)


def make_accepter(
    client: FakeAcceptanceClient,
    *,
    sleeps: list[float] | None = None,
    logs: list[str] | None = None,
) -> AutoAccepter:
    sleep_calls = sleeps if sleeps is not None else []
    log_lines = logs if logs is not None else []
    return AutoAccepter(
        client,
        timezone=DENVER,
        now_unix=lambda: NOW,
        sleep=lambda seconds: _record_sleep(sleep_calls, seconds),
        log=log_lines.append,
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
        "STANDLEY LAKE HS",
        "Standley Lake High School",
    ],
)
def test_only_the_four_normalized_target_schools_qualify(school: str) -> None:
    assert is_auto_accept_eligible(job(location_name=school), NOW, DENVER)


@pytest.mark.parametrize(
    "school",
    ["WHEAT RIDGE HIGH SCHOOL", "ARVADA HIGH SCHOOL", "LAKEWOOD HIGH SCHOOL"],
)
def test_other_high_schools_do_not_qualify(school: str) -> None:
    assert not is_auto_accept_eligible(job(location_name=school), NOW, DENVER)


@pytest.mark.parametrize(
    "subject",
    [
        None,
        "",
        "ENGLISH",
        "SEC SOCIAL STUDIES",
        "SEC FAMILY CONSUMER",
        "FAMILY AND CONSUMER SCIENCE",
        "FAMILY & CONSUMER SCIENCE",
        "AFTERMATH",
        "CONSCIENCE",
    ],
)
def test_non_math_or_science_subjects_fail_closed(subject: str | None) -> None:
    assert not is_auto_accept_eligible(job(classf_name=subject), NOW, DENVER)


@pytest.mark.parametrize(
    "subject",
    [
        "SEC MATH",
        "MATHEMATICS",
        "ALGEBRA I",
        "GEOMETRY",
        "CALCULUS",
        "STATISTICS",
        "TRIGONOMETRY",
        "SEC SCIENCE",
        "NATURAL SCIENCES",
        "BIOLOGY",
        "CHEMISTRY",
        "PHYSICS",
        "ASTRONOMY",
        "GEOLOGY",
        "ECOLOGY",
        "ENVIRONMENTAL SCIENCE",
        "EARTH SCIENCE",
        "COMPUTER SCIENCE",
        "SOCIAL SCIENCE",
    ],
)
def test_math_and_all_science_subjects_qualify(subject: str) -> None:
    assert is_auto_accept_eligible(job(classf_name=subject), NOW, DENVER)


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


async def test_retries_transient_failures_at_one_and_three_seconds() -> None:
    client = FakeAcceptanceClient(
        [SfeHttpError(500, "HTTP 500"), SfeHttpError(429, "HTTP 429"), None],
        [False, False],
    )
    sleeps: list[float] = []
    accepter = AutoAccepter(
        client,
        timezone=DENVER,
        now_unix=lambda: NOW,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
        log=lambda _: None,
    )
    [item] = await accepter.reconcile([job()])
    assert isinstance(item, AutoAcceptItem)
    assert item.outcome.status is AutoAcceptStatus.ACCEPTED
    assert client.accepted_ids == [101, 101, 101]
    assert client.verified_ids == [101, 101]
    assert sleeps == [1.0, 3.0]


async def test_ambiguous_failure_is_success_when_assignment_is_visible() -> None:
    client = FakeAcceptanceClient([httpx.ReadTimeout("timed out")], [True])
    accepter = make_accepter(client)
    [item] = await accepter.reconcile([job()])
    assert isinstance(item, AutoAcceptItem)
    assert item.outcome.status is AutoAcceptStatus.ACCEPTED
    assert client.accepted_ids == [101]


async def test_permanent_4xx_verifies_then_stops_without_sleeping() -> None:
    client = FakeAcceptanceClient([SfeHttpError(400, "private")], [False])
    sleeps: list[float] = []
    accepter = make_accepter(client, sleeps=sleeps)
    [item] = await accepter.reconcile([job()])
    assert isinstance(item, AutoAcceptItem)
    assert item.outcome == AutoAcceptOutcome(AutoAcceptStatus.FAILED, "HTTP 400")
    assert client.accepted_ids == [101]
    assert sleeps == []


async def test_three_transient_failures_produce_one_failed_outcome() -> None:
    client = FakeAcceptanceClient(
        [SfeHttpError(500, "x"), SfeHttpError(500, "y"), SfeHttpError(500, "z")],
        [False, False, False],
    )
    sleeps: list[float] = []
    [item] = await make_accepter(client, sleeps=sleeps).reconcile([job()])
    assert isinstance(item, AutoAcceptItem)
    assert item.outcome == AutoAcceptOutcome(AutoAcceptStatus.FAILED, "HTTP 500")
    assert client.accepted_ids == [101, 101, 101]
    assert client.verified_ids == [101, 101, 101]
    assert sleeps == [1.0, 3.0]


async def test_verification_error_is_sanitized_and_keeps_the_put_failure() -> None:
    logs: list[str] = []
    client = FakeAcceptanceClient(
        [SfeHttpError(400, "PRIVATE PUT BODY")],
        [RuntimeError("PRIVATE VERIFY BODY")],
    )
    [item] = await make_accepter(client, logs=logs).reconcile([job()])
    assert isinstance(item, AutoAcceptItem)
    assert item.outcome == AutoAcceptOutcome(AutoAcceptStatus.FAILED, "HTTP 400")
    rendered = "\n".join(logs)
    assert "PRIVATE" not in rendered
    assert "RuntimeError" in rendered


async def test_continuously_visible_job_reuses_the_cached_decision() -> None:
    client = FakeAcceptanceClient([None])
    accepter = make_accepter(client)
    first = await accepter.reconcile([job()])
    second = await accepter.reconcile([job()])
    assert first == second
    assert client.accepted_ids == [101]


async def test_temporarily_ineligible_visible_job_keeps_cached_decision() -> None:
    client = FakeAcceptanceClient([None])
    accepter = make_accepter(client)
    eligible = job()
    temporarily_ineligible = job(location_name="WHEAT RIDGE HIGH SCHOOL")

    [first] = await accepter.reconcile([eligible])
    [second] = await accepter.reconcile([temporarily_ineligible])
    [third] = await accepter.reconcile([eligible])

    assert isinstance(first, AutoAcceptItem)
    assert second is first
    assert third is first
    assert client.accepted_ids == [101]


async def test_disappearance_then_return_starts_a_new_sequence() -> None:
    client = FakeAcceptanceClient([None, None])
    accepter = make_accepter(client)
    await accepter.reconcile([job()])
    assert await accepter.reconcile([]) == []
    await accepter.reconcile([job()])
    assert client.accepted_ids == [101, 101]


async def test_fresh_reconciler_retries_a_current_job() -> None:
    first = FakeAcceptanceClient([None])
    second = FakeAcceptanceClient([None])
    await make_accepter(first).reconcile([job()])
    await make_accepter(second).reconcile([job()])
    assert first.accepted_ids == [101]
    assert second.accepted_ids == [101]


async def test_ineligible_job_passes_through_without_network_calls() -> None:
    candidate = job(location_name="WHEAT RIDGE HIGH SCHOOL")
    client = FakeAcceptanceClient([])
    [result] = await make_accepter(client).reconcile([candidate])
    assert result is candidate
    assert client.accepted_ids == []


async def test_ineligible_duplicate_id_passes_through_unchanged() -> None:
    ordinary = job(location_name="WHEAT RIDGE HIGH SCHOOL")
    eligible = job()
    client = FakeAcceptanceClient([None])

    results = await make_accepter(client).reconcile([ordinary, eligible])

    assert results[0] is ordinary
    assert isinstance(results[1], AutoAcceptItem)
    assert results[1].job is eligible
    assert client.accepted_ids == [101]


async def test_duplicate_eligible_rows_emit_one_outcome_in_input_order() -> None:
    ordinary_a = job(job_id=1, location_name="WHEAT RIDGE HIGH SCHOOL")
    eligible = job(job_id=2)
    duplicate = job(job_id=2)
    ordinary_b = job(job_id=3, location_name="LAKEWOOD HIGH SCHOOL")
    client = FakeAcceptanceClient([None])

    results = await make_accepter(client).reconcile([ordinary_a, eligible, duplicate, ordinary_b])

    assert len(results) == 3
    assert results[0] is ordinary_a
    assert isinstance(results[1], AutoAcceptItem)
    assert results[1].job is eligible
    assert results[2] is ordinary_b
    assert client.accepted_ids == [2]


async def test_reconcile_preserves_mixed_input_order() -> None:
    ordinary_a = job(job_id=1, location_name="WHEAT RIDGE HIGH SCHOOL")
    eligible = job(job_id=2)
    ordinary_b = job(job_id=3, location_name="LAKEWOOD HIGH SCHOOL")
    client = FakeAcceptanceClient([None])
    results = await make_accepter(client).reconcile([ordinary_a, eligible, ordinary_b])
    assert results[0] is ordinary_a
    assert isinstance(results[1], AutoAcceptItem)
    assert results[1].job is eligible
    assert results[2] is ordinary_b
