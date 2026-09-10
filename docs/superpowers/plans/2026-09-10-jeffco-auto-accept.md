# Jeffco Automatic Job Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Automatically accept full-day Jeffco substitute jobs at Ralston Valley, Golden, or Arvada West when the first work date is at least seven Denver calendar days away, then report the outcome to Discord.

**Architecture:** Add a Jeffco-only reconciler between the existing high-school filter and shared runner. It makes a bounded, verified acceptance attempt once per continuous visibility episode and wraps only attempted jobs with an outcome-specific state key; the shared runner and all other monitors remain unchanged.

**Tech Stack:** Python 3.13, asyncio, httpx, pytest/pytest-asyncio, mypy strict mode, Ruff, Terraform.

**Spec:** docs/superpowers/specs/2026-09-10-jeffco-auto-accept-design.md

## Global Constraints

- Automatic acceptance is enabled only when AUTO_ACCEPT_ENABLED=true; the default is false.
- The target schools are exactly RALSTON VALLEY HS, GOLDEN HS, and ARVADA WEST HS after existing school-name normalization.
- duration_type must equal FULL exactly.
- job_start must contain an explicit UTC offset, and its Denver date must be at least today plus seven calendar days.
- Eligibility fails closed. Unexpected data remains an ordinary alert-only job.
- Make at most three PUT attempts per continuous visibility episode, waiting one second before attempt two and three seconds before attempt three.
- Verify GET /api/jobAssignment/{job_id} after every failed PUT before retrying or reporting failure.
- Never delay the acceptance PUT on Discord delivery.
- Never include SFE response bodies, credentials, cookies, authorization values, or redirect headers in acceptance errors or logs.
- Every HTTP send continues through SfeClient._send.
- Do not change the shared Monitor protocol or runner.
- Do not add Python-only outcome payloads to the TypeScript parity fixture set.
- Do not modify the real infra/terraform.tfvars, Terraform state, runtime state, images, or production without separate explicit authorization.

## File Structure

- Create apps/jeffco/src/jeffco/auto_accept.py: eligibility, outcome types, retry classification, visibility cache, and reconciler.
- Create tests/jeffco/test_auto_accept.py: pure rule and reconciler coverage.
- Modify apps/jeffco/src/jeffco/sfe.py: shared authenticated response helper, bodyless acceptance PUT, and assignment verification GET.
- Modify tests/jeffco/test_sfe_client.py: exact wire-shape, auth, response, and credential-safety tests.
- Modify apps/jeffco/src/jeffco/alert.py: accepted and failed outcome payloads without changing ordinary payloads.
- Modify tests/jeffco/test_alert.py: exact outcome payload assertions and ordinary-payload regression coverage.
- Modify apps/jeffco/src/jeffco/monitor.py: invoke the reconciler, key outcome items, and route outcome rendering.
- Modify tests/jeffco/test_monitor.py: fetch/key/render integration and detail-failure degradation.
- Modify apps/jeffco/src/jeffco/config.py and tests/jeffco/test_config.py: disabled-by-default feature flag.
- Modify apps/jeffco/src/jeffco/main.py: construct the reconciler only when enabled.
- Modify infra/variables.tf, infra/main.tf, and infra/terraform.tfvars.example: pass the disabled-by-default flag to Jeffco.
- Modify docs/architecture.md and docs/invariants.md: document the mutation boundary and load-bearing rules.

---

### Task 1: Eligibility and Outcome Domain

**Files:**

- Create: apps/jeffco/src/jeffco/auto_accept.py
- Create: tests/jeffco/test_auto_accept.py

**Interfaces:**

- Produces: AutoAcceptStatus, AutoAcceptOutcome, AutoAcceptItem, and JeffcoItem.
- Produces: is_auto_accept_eligible(job: Job, now_unix: int, timezone: str) -> bool.
- Produces: auto_accept_key(item: AutoAcceptItem) -> str.
- Consumes: Job and normalize_school_name from the existing Jeffco app.

- [ ] **Step 1: Write failing eligibility tests**

Create tests/jeffco/test_auto_accept.py with pinned Denver times and a helper that defaults to a qualifying job:

~~~python
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
~~~

Add the DST boundary explicitly:

~~~python
def test_lead_time_uses_denver_dates_across_fall_back() -> None:
    now = int(datetime(2026, 10, 26, 23, 30, tzinfo=ZoneInfo(DENVER)).timestamp())
    assert is_auto_accept_eligible(
        job(job_start="2026-11-02T07:00:00Z"),
        now,
        DENVER,
    )
~~~

- [ ] **Step 2: Run the focused tests and confirm the missing-module failure**

Run:

~~~bash
just test tests/jeffco/test_auto_accept.py -k "qualify or timestamp"
~~~

Expected: collection fails because jeffco.auto_accept does not exist.

- [ ] **Step 3: Implement the domain types and pure predicate**

Create apps/jeffco/src/jeffco/auto_accept.py with these definitions:

~~~python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from jeffco.schools import normalize_school_name
from jeffco.types import Job

AUTO_ACCEPT_MIN_DAYS = 7
AUTO_ACCEPT_SCHOOLS = frozenset(
    normalize_school_name(name)
    for name in ("RALSTON VALLEY HS", "GOLDEN HS", "ARVADA WEST HS")
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
~~~

- [ ] **Step 4: Add outcome-key tests**

Append tests proving accepted and failed namespaces are stable and distinct from the ordinary numeric job key:

~~~python
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
~~~

- [ ] **Step 5: Run and format the focused task**

Run:

~~~bash
just test tests/jeffco/test_auto_accept.py
just typecheck
just lint
just format-check
~~~

Expected: all pass.

- [ ] **Step 6: Commit the eligibility domain**

~~~bash
git add apps/jeffco/src/jeffco/auto_accept.py tests/jeffco/test_auto_accept.py
git commit -m "Add Jeffco auto-accept eligibility rules"
~~~

---

### Task 2: Status-Only SFE Acceptance Operations

**Files:**

- Modify: apps/jeffco/src/jeffco/sfe.py:559-635
- Modify: tests/jeffco/test_sfe_client.py:192-380, 595-665

**Interfaces:**

- Produces: SfeClient.accept_job(job_id: int) -> None.
- Produces: SfeClient.has_job_assignment(job_id: int) -> bool.
- Preserves: fetch_available_jobs and fetch_job_detail return and error behavior.
- Preserves: _send as the only direct httpx client access.

- [ ] **Step 1: Write the exact acceptance-wire tests**

Add a plain empty-response helper and tests to tests/jeffco/test_sfe_client.py:

~~~python
def empty_response(status: int = 204) -> httpx.Response:
    return httpx.Response(status)


async def test_accepts_a_job_with_the_public_spas_exact_request_shape() -> None:
    async with client([*login_responses(), empty_response()]) as h:
        await h.sfe.accept_job(1025535)

    call = h.calls[-1]
    assert call.method == "PUT"
    assert call.url == f"{SFE_BASE}/api/jobAssignment/1025535"
    assert call.body == ""
    assert call.headers["authorization"].startswith("Bearer eyJ")


async def test_acceptance_ignores_empty_and_json_success_bodies() -> None:
    async with client(
        [*login_responses(), json_response({"ignored": True}), empty_response()]
    ) as h:
        await h.sfe.accept_job(1)
        await h.sfe.accept_job(2)
    assert [call.method for call in h.calls[-2:]] == ["PUT", "PUT"]
~~~

- [ ] **Step 2: Write authentication, verification, and sanitization tests**

Add tests for:

~~~python
async def test_acceptance_reauthenticates_once_after_401() -> None:
    async with client(
        [
            *login_responses(),
            json_response({"secret": "do-not-log"}, 401),
            *login_responses(),
            empty_response(),
        ]
    ) as h:
        await h.sfe.accept_job(1025535)
    puts = [call for call in h.calls if "/api/jobAssignment/" in call.url]
    assert len(puts) == 2
    assert h.logins == 2


async def test_assignment_verification_uses_the_active_assignment_endpoint() -> None:
    async with client([*login_responses(), json_response({"jobId": 1025535})]) as h:
        assert await h.sfe.has_job_assignment(1025535) is True
    assert h.calls[-1].method == "GET"
    assert h.calls[-1].url == f"{SFE_BASE}/api/jobAssignment/1025535"


async def test_assignment_verification_treats_404_as_not_assigned() -> None:
    async with client([*login_responses(), json_response({"secret": "hidden"}, 404)]) as h:
        assert await h.sfe.has_job_assignment(1025535) is False


async def test_acceptance_error_never_quotes_the_response_body() -> None:
    async with client(
        [*login_responses(), json_response({"message": "PRIVATE BODY"}, 400)]
    ) as h:
        with pytest.raises(SfeHttpError, match="HTTP 400") as exc:
            await h.sfe.accept_job(1025535)
    assert "PRIVATE BODY" not in str(exc.value)
~~~

Replay the existing HTML credential-leak case through accept_job:

~~~python
async def test_acceptance_html_error_does_not_leak_a_session_id() -> None:
    page = f'<html><form action="/x;jsessionid={SESSION_SENTINEL}">'
    async with client([*login_responses(), html(page, 500)]) as h:
        with pytest.raises(SfeShapeError, match=r"(?i)html") as exc:
            await h.sfe.accept_job(1025535)
    rendered = "\n".join([str(exc.value), repr(exc.value)])
    assert SESSION_SENTINEL not in rendered
    assert "jsessionid" not in rendered
~~~

- [ ] **Step 3: Run the new SFE tests and confirm failure**

Run:

~~~bash
just test tests/jeffco/test_sfe_client.py -k "acceptance or assignment_verification"
~~~

Expected: failures because accept_job and has_job_assignment do not exist.

- [ ] **Step 4: Factor authenticated response handling**

In SfeClient, extract the token/send/401/HTML/status portion of _api_request into:

~~~python
async def _authenticated_response(
    self,
    path: str,
    method: str,
    json_body: object | None,
    label: str,
    *,
    allow_retry: bool = True,
    include_api_message: bool = True,
) -> httpx.Response:
    await self._ensure_token()
    response = await self._send(
        method,
        f"{SFE_BASE}{path}",
        label,
        json_body=json_body,
        headers={**BROWSER_HEADERS, "authorization": f"Bearer {self._token}"},
    )
    text = response.text
    if response.status_code == 401 and allow_retry:
        self._log(f"{label}: 401, re-authenticating once and retrying")
        self._token = None
        return await self._authenticated_response(
            path,
            method,
            json_body,
            label,
            allow_retry=False,
            include_api_message=include_api_message,
        )
    _assert_not_html(text, response.headers.get("content-type"), label, response.status_code)
    if not response.is_success:
        detail = _api_message(text) if include_api_message else ""
        raise SfeHttpError(
            response.status_code,
            f"SFE {label} failed: HTTP {response.status_code}{detail}",
        )
    return response
~~~

Change _api_request to call this helper with include_api_message=True and retain its existing JSON parsing and exact unparseable-JSON error. Do not change _send.

- [ ] **Step 5: Implement status-only public methods**

Add:

~~~python
async def accept_job(self, job_id: int) -> None:
    await self._authenticated_response(
        f"/api/jobAssignment/{job_id}",
        "PUT",
        None,
        f"job acceptance {job_id}",
        include_api_message=False,
    )


async def has_job_assignment(self, job_id: int) -> bool:
    try:
        await self._authenticated_response(
            f"/api/jobAssignment/{job_id}",
            "GET",
            None,
            f"job assignment verification {job_id}",
            include_api_message=False,
        )
    except SfeHttpError as err:
        if err.status == 404:
            return False
        raise
    return True
~~~

- [ ] **Step 6: Run focused and full SFE tests**

Run:

~~~bash
just test tests/jeffco/test_sfe_client.py -k "acceptance or assignment_verification or 401 or response"
just test tests/jeffco/test_sfe_client.py tests/jeffco/test_sfe_pure.py
just typecheck
~~~

Expected: all pass, including the single-funnel AST guard.

- [ ] **Step 7: Commit the SFE operation**

~~~bash
git add apps/jeffco/src/jeffco/sfe.py tests/jeffco/test_sfe_client.py
git commit -m "Add authenticated SFE job acceptance"
~~~

---

### Task 3: Bounded Retry and Visibility Reconciliation

**Files:**

- Modify: apps/jeffco/src/jeffco/auto_accept.py
- Modify: tests/jeffco/test_auto_accept.py

**Interfaces:**

- Consumes: an AcceptanceClient Protocol with accept_job and has_job_assignment.
- Produces: AutoAccepter.reconcile(jobs: Sequence[Job]) -> list[JeffcoItem].
- Preserves input order and passes every ineligible Job through unchanged.

- [ ] **Step 1: Add a strictly typed fake acceptance client**

In tests/jeffco/test_auto_accept.py add:

~~~python
from collections.abc import Awaitable, Callable, Sequence

import httpx
from jeffco.auto_accept import AutoAccepter
from jeffco.sfe import SfeHttpError


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
~~~

- [ ] **Step 2: Write retry, verification, and cache tests**

Add these behaviors with exact assertions:

~~~python
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
~~~

Use these exact helpers:

~~~python
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
~~~

Add the remaining state and failure cases:

~~~python
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
~~~

- [ ] **Step 3: Run the reconciler tests and confirm failure**

Run:

~~~bash
just test tests/jeffco/test_auto_accept.py -k "retries or failure or visible or reconcile"
~~~

Expected: failures because AutoAccepter does not exist.

- [ ] **Step 4: Implement the client protocol and retry helpers**

Add to auto_accept.py:

~~~python
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

import httpx

from jeffco.sfe import SfeHttpError

type Sleeper = Callable[[float], Awaitable[None]]

RETRY_DELAYS = (1.0, 3.0)


class AcceptanceClient(Protocol):
    async def accept_job(self, job_id: int) -> None: ...

    async def has_job_assignment(self, job_id: int) -> bool: ...


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, SfeHttpError):
        return error.status in {408, 429} or 500 <= error.status <= 599
    return isinstance(error, httpx.TransportError)


def _failure_detail(error: Exception) -> str:
    if isinstance(error, SfeHttpError):
        return f"HTTP {error.status}"
    if isinstance(error, httpx.TimeoutException):
        return "request timed out"
    if isinstance(error, httpx.TransportError):
        return "transport error"
    return type(error).__name__
~~~

- [ ] **Step 5: Implement one sequence per visibility episode**

Implement AutoAccepter with the approved flow:

~~~python
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
        eligible = {
            candidate.job_id
            for candidate in jobs
            if is_auto_accept_eligible(candidate, now, self._timezone)
        }
        self._decisions = {
            job_id: decision
            for job_id, decision in self._decisions.items()
            if job_id in eligible
        }
        reconciled: list[JeffcoItem] = []
        for candidate in jobs:
            if candidate.job_id not in eligible:
                reconciled.append(candidate)
                continue
            decision = self._decisions.get(candidate.job_id)
            if decision is None:
                decision = await self._attempt(candidate)
                self._decisions[candidate.job_id] = decision
            reconciled.append(decision)
        return reconciled
~~~

Implement the sequence exactly:

~~~python
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
~~~

- [ ] **Step 6: Run and validate the reconciler**

Run:

~~~bash
just test tests/jeffco/test_auto_accept.py
just typecheck
just lint
just format-check
~~~

Expected: all pass.

- [ ] **Step 7: Commit the reconciler**

~~~bash
git add apps/jeffco/src/jeffco/auto_accept.py tests/jeffco/test_auto_accept.py
git commit -m "Reconcile eligible Jeffco jobs with bounded retries"
~~~

---

### Task 4: Discord Outcome Payloads

**Files:**

- Modify: apps/jeffco/src/jeffco/alert.py:46-90
- Modify: tests/jeffco/test_alert.py:54-190

**Interfaces:**

- Produces: format_auto_accept_alert(item: AutoAcceptItem, date_line: str) -> Message.
- Consumes: auto_accept_key and the existing _describe job-detail formatter.
- Preserves: format_job_alerts output byte for byte.

- [ ] **Step 1: Write exact accepted and failed payload tests**

Add imports for AutoAcceptItem, AutoAcceptOutcome, and AutoAcceptStatus, then add:

~~~python
from monitor.types import GREEN, RED


def test_formats_an_automatic_acceptance_confirmation() -> None:
    item = AutoAcceptItem(
        _job(
            job_id=1025535,
            location_name="RALSTON VALLEY HS",
            classf_name="SEC MATH",
            employee_first_name="Jamie",
            employee_last_name="Placeholder",
            duration_type="FULL",
        ),
        AutoAcceptOutcome(AutoAcceptStatus.ACCEPTED),
    )
    message = format_auto_accept_alert(item, DATE_LINE)
    embed = message.payload.embeds[0]
    assert embed.title == "🏫 RALSTON VALLEY HS"
    assert embed.description == (
        "**Status:** Automatically accepted\n"
        "**Subject:** SEC MATH\n"
        f"**Dates:** {DATE_LINE}\n"
        "**Teacher:** Jamie Placeholder\n"
        "**Job ID:** 1025535"
    )
    assert embed.color == GREEN
    assert embed.url == AVAILABLE_JOBS_URL
    assert embed.footer_text == "This job was accepted automatically."
    assert message.covers == ("auto-accepted:1025535",)


def test_formats_an_urgent_manual_claim_failure() -> None:
    item = AutoAcceptItem(
        _job(job_id=1025535, duration_type="FULL"),
        AutoAcceptOutcome(AutoAcceptStatus.FAILED, "HTTP 400"),
    )
    message = format_auto_accept_alert(item, DATE_LINE)
    embed = message.payload.embeds[0]
    assert embed.description is not None
    assert "**Status:** Auto-accept failed — claim manually" in embed.description
    assert "**Failure:** HTTP 400" in embed.description
    assert "**Job ID:** 1025535" in embed.description
    assert embed.color == RED
    assert embed.footer_text == "Tap the title and claim this job manually now."
    assert message.covers == ("auto-failed:1025535",)
~~~

Add the payload-safety and FULL-duration assertion:

~~~python
def test_auto_accept_outcomes_never_ping_and_do_not_repeat_full_duration() -> None:
    for outcome in (
        AutoAcceptOutcome(AutoAcceptStatus.ACCEPTED),
        AutoAcceptOutcome(AutoAcceptStatus.FAILED, "HTTP 400"),
    ):
        message = format_auto_accept_alert(
            AutoAcceptItem(_job(duration_type="FULL"), outcome),
            DATE_LINE,
        )
        assert message.payload.content is None
        assert message.payload.allowed_mentions_parse is None
        description = message.payload.embeds[0].description
        assert description is not None
        assert "Duration" not in description
~~~

- [ ] **Step 2: Confirm the formatter is absent**

Run:

~~~bash
just test tests/jeffco/test_alert.py -k "automatic or manual_claim"
~~~

Expected: import or name failure for format_auto_accept_alert.

- [ ] **Step 3: Implement the outcome formatter**

Keep _describe and format_job_alerts unchanged. Extend the imports with:

~~~python
from jeffco.auto_accept import (
    AutoAcceptItem,
    AutoAcceptStatus,
    auto_accept_key,
)
from monitor.types import GREEN, RED, Embed, HeartbeatExtras, Message, Payload
~~~

Then add a single-item formatter:

~~~python
def format_auto_accept_alert(item: AutoAcceptItem, date_line: str) -> Message:
    accepted = item.outcome.status is AutoAcceptStatus.ACCEPTED
    status = (
        "Automatically accepted"
        if accepted
        else "Auto-accept failed — claim manually"
    )
    lines = [f"**Status:** {status}", _describe(item.job, date_line)]
    lines.append(f"**Job ID:** {item.job.job_id}")
    if item.outcome.detail is not None:
        lines.append(f"**Failure:** {item.outcome.detail}")
    return Message(
        payload=Payload(
            embeds=(
                Embed(
                    title=f"🏫 {item.job.location_name}",
                    url=AVAILABLE_JOBS_URL,
                    description="\n".join(lines),
                    color=GREEN if accepted else RED,
                    footer_text=(
                        "This job was accepted automatically."
                        if accepted
                        else "Tap the title and claim this job manually now."
                    ),
                ),
            )
        ),
        covers=(auto_accept_key(item),),
    )
~~~

- [ ] **Step 4: Prove ordinary payload parity is untouched**

Run:

~~~bash
just test tests/jeffco/test_alert.py
just parity
~~~

Expected: all alert tests pass and the existing Jeffco parity diff is empty. Do not add accepted or failed payloads to tools/dump_payloads_jeffco.py.

- [ ] **Step 5: Commit outcome rendering**

~~~bash
git add apps/jeffco/src/jeffco/alert.py tests/jeffco/test_alert.py
git commit -m "Render Jeffco auto-accept outcomes"
~~~

---

### Task 5: Jeffco Monitor Integration

**Files:**

- Modify: apps/jeffco/src/jeffco/monitor.py:1-130
- Modify: tests/jeffco/test_monitor.py:15-301

**Interfaces:**

- Consumes: AutoAccepter.reconcile, JeffcoItem, auto_accept_key, and format_auto_accept_alert.
- Changes JeffcoMonitor to implement Monitor[JeffcoItem].
- Preserves ordinary Job keys and ordinary render payloads.

- [ ] **Step 1: Extend the monitor test harness without enabling acceptance by default**

Update make_monitor with an optional real reconciler around its existing SfeClient:

~~~python
async def _record_sleep(sleeps: list[float], seconds: float) -> None:
    sleeps.append(seconds)


def make_monitor(
    responses: list[httpx.Response],
    logs: list[str] | None = None,
    *,
    enable_auto_accept: bool = False,
    sleeps: list[float] | None = None,
) -> tuple[JeffcoMonitor, httpx.AsyncClient]:
    sink = logs if logs is not None else []
    sleep_calls = sleeps if sleeps is not None else []
    cfg = load_config(
        {"SFE_USER_ID": USER_ID, "SFE_PIN": PIN, "DISCORD_WEBHOOK_URL": WEBHOOK}
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(_queue(responses)))
    sfe = SfeClient(
        client=client,
        user_id=cfg.sfe_user_id,
        pin=cfg.sfe_pin,
        timezone=cfg.timezone,
        window_days=cfg.window_days,
        now_unix=lambda: NOW,
        log=sink.append,
    )
    auto_accepter = (
        AutoAccepter(
            sfe,
            timezone=cfg.timezone,
            now_unix=lambda: NOW,
            sleep=lambda seconds: _record_sleep(sleep_calls, seconds),
            log=sink.append,
        )
        if enable_auto_accept
        else None
    )
    monitor = JeffcoMonitor(
        cfg,
        sfe,
        log=sink.append,
        now_unix=lambda: NOW,
        auto_accepter=auto_accepter,
    )
    return monitor, client
~~~

Add these tests:

~~~python
def test_outcome_key_is_distinct_from_a_previously_seen_job_id() -> None:
    monitor, _ = make_monitor([])
    base = Job(
        job_id=1025535,
        location_name="RALSTON VALLEY HS",
        job_start="2026-10-16T13:45Z",
        job_end="2026-10-16T21:30Z",
        duration_type="FULL",
    )
    accepted = AutoAcceptItem(
        base,
        AutoAcceptOutcome(AutoAcceptStatus.ACCEPTED),
    )
    assert monitor.key(base) == "1025535"
    assert monitor.key(accepted) == "auto-accepted:1025535"


async def test_fetch_runs_auto_accept_after_high_school_filtering() -> None:
    row = _job(101, "RALSTON VALLEY HS")
    row["jobStart"] = "2026-10-16T13:45Z"
    row["jobEnd"] = "2026-10-16T21:30Z"
    row["durationType"] = "FULL"
    monitor, client = make_monitor(
        [*_login_responses(), _json_response([row]), httpx.Response(204)],
        enable_auto_accept=True,
    )
    async with client:
        items = await monitor.fetch()
    assert isinstance(items[0], AutoAcceptItem)
    assert monitor.key(items[0]) == "auto-accepted:101"
~~~

The explicit row values above are full-day and more than seven Denver calendar days after NOW. The queued 204 is the bodyless PUT response.

- [ ] **Step 2: Write mixed render and degradation tests**

Add the mixed rendering case:

~~~python
async def test_render_preserves_mixed_ordinary_and_outcome_order() -> None:
    ordinary = Job(
        job_id=1,
        location_name="WHEAT RIDGE HIGH SCHOOL",
        job_start="2026-09-04T13:45Z",
        job_end="2026-09-04T21:30Z",
    )
    accepted_job = Job(
        job_id=2,
        location_name="GOLDEN HIGH SCHOOL",
        job_start="2026-10-16T13:45Z",
        job_end="2026-10-16T21:30Z",
        duration_type="FULL",
    )
    accepted = AutoAcceptItem(
        accepted_job,
        AutoAcceptOutcome(AutoAcceptStatus.ACCEPTED),
    )
    monitor, client = make_monitor(
        [*_login_responses(), _json_response(SINGLE), _json_response(MULTIDAY)]
    )
    async with client:
        messages = await monitor.render([ordinary, accepted])
    assert [message.covers for message in messages] == [
        ("1",),
        ("auto-accepted:2",),
    ]
    assert "Automatically accepted" not in (
        messages[0].payload.embeds[0].description or ""
    )
    assert "Automatically accepted" in (
        messages[1].payload.embeds[0].description or ""
    )


async def test_outcome_detail_failure_keeps_the_irreversible_status() -> None:
    accepted = AutoAcceptItem(
        Job(
            job_id=2,
            location_name="GOLDEN HIGH SCHOOL",
            job_start="2026-10-16T13:45Z",
            job_end="2026-10-16T21:30Z",
            duration_type="FULL",
        ),
        AutoAcceptOutcome(AutoAcceptStatus.ACCEPTED),
    )
    monitor, client = make_monitor(
        [*_login_responses(), _json_response({"message": "boom"}, 500)]
    )
    async with client:
        [message] = await monitor.render([accepted])
    description = message.payload.embeds[0].description or ""
    assert "Automatically accepted" in description
    assert "approximate, check SFE" in description
~~~

- [ ] **Step 3: Run the integration tests and confirm failure**

Run:

~~~bash
just test tests/jeffco/test_monitor.py -k "auto_accept or outcome or mixed"
~~~

Expected: failures because JeffcoMonitor still accepts only Job and has no reconciler.

- [ ] **Step 4: Integrate the optional reconciler into fetch and key**

Import AutoAcceptItem, AutoAccepter, JeffcoItem, auto_accept_key, and
format_auto_accept_alert from their Task 1-4 modules.

Update JeffcoMonitor.__init__ with:

~~~python
auto_accepter: AutoAccepter | None = None
~~~

Store it as self._auto_accepter. Change fetch to return list[JeffcoItem], and after the existing partition/gap logic:

~~~python
if self._auto_accepter is None:
    items: list[JeffcoItem] = list(hs)
    return items
return await self._auto_accepter.reconcile(hs)
~~~

Change key to:

~~~python
def key(self, item: JeffcoItem) -> str:
    if isinstance(item, AutoAcceptItem):
        return auto_accept_key(item)
    return str(item.job_id)
~~~

- [ ] **Step 5: Route each item through ordinary or outcome rendering**

Change render to preserve input order:

~~~python
async def render(self, new: list[JeffcoItem]) -> list[Message]:
    messages: list[Message] = []
    for item in new:
        job = item.job if isinstance(item, AutoAcceptItem) else item
        date_line = await self._resolve_date_line(job)
        if isinstance(item, AutoAcceptItem):
            messages.append(format_auto_accept_alert(item, date_line))
        else:
            messages.extend(format_job_alerts([(job, date_line)]))
    return messages
~~~

Update the compile-time assertion exactly, while keeping _resolve_date_line accepting Job:

~~~python
if TYPE_CHECKING:

    def _assert_satisfies_protocol(m: JeffcoMonitor) -> Monitor[JeffcoItem]:
        return m
~~~

- [ ] **Step 6: Run all Jeffco monitor and alert tests**

Run:

~~~bash
just test tests/jeffco/test_monitor.py tests/jeffco/test_alert.py tests/jeffco/test_auto_accept.py
just typecheck
~~~

Expected: all pass.

- [ ] **Step 7: Commit monitor integration**

~~~bash
git add apps/jeffco/src/jeffco/monitor.py tests/jeffco/test_monitor.py
git commit -m "Integrate auto-accept with the Jeffco monitor"
~~~

---

### Task 6: Disabled-by-Default Configuration and Infrastructure

**Files:**

- Modify: apps/jeffco/src/jeffco/config.py:8-69
- Modify: tests/jeffco/test_config.py:20-45
- Modify: apps/jeffco/src/jeffco/main.py:8-58
- Modify: infra/variables.tf:105-165
- Modify: infra/main.tf:72-84
- Modify: infra/terraform.tfvars.example: Jeffco section

**Interfaces:**

- Produces: JeffcoConfig.auto_accept_enabled: bool.
- Consumes: monitor.config.env_bool.
- Produces: Terraform variable jeffco_auto_accept_enabled with default false.
- Produces: AUTO_ACCEPT_ENABLED in only the Jeffco container environment.

- [ ] **Step 1: Write config tests for safe defaults and explicit enablement**

Add:

~~~python
def test_auto_accept_is_disabled_by_default() -> None:
    assert load_config(BASE).auto_accept_enabled is False


def test_auto_accept_can_be_explicitly_enabled() -> None:
    cfg = load_config({**BASE, "AUTO_ACCEPT_ENABLED": "true"})
    assert cfg.auto_accept_enabled is True


def test_auto_accept_uses_strict_shared_boolean_validation() -> None:
    with pytest.raises(ConfigError, match="AUTO_ACCEPT_ENABLED"):
        load_config({**BASE, "AUTO_ACCEPT_ENABLED": "sometimes"})
~~~

- [ ] **Step 2: Confirm config tests fail**

Run:

~~~bash
just test tests/jeffco/test_config.py -k auto_accept
~~~

Expected: missing JeffcoConfig field.

- [ ] **Step 3: Add the application flag**

Import env_bool, add auto_accept_enabled: bool to JeffcoConfig, and load:

~~~python
auto_accept_enabled=env_bool(env, "AUTO_ACCEPT_ENABLED", False),
~~~

Update _main to construct the reconciler only when enabled:

~~~python
auto_accepter = (
    AutoAccepter(
        sfe,
        timezone=cfg.timezone,
        now_unix=system_now,
        sleep=asyncio.sleep,
        log=log,
    )
    if cfg.auto_accept_enabled
    else None
)
monitor = JeffcoMonitor(
    cfg,
    sfe,
    log=log,
    now_unix=system_now,
    auto_accepter=auto_accepter,
)
~~~

Include auto_accept=enabled or auto_accept=disabled in the existing safe startup config log. Do not log target names, user ID, PIN, or tokens.

- [ ] **Step 4: Add Terraform wiring with a false default**

In infra/variables.tf, add beside the Jeffco variables:

~~~hcl
variable "jeffco_auto_accept_enabled" {
  description = "Whether Jeffco may automatically accept narrowly eligible jobs."
  type        = bool
  default     = false
}
~~~

In infra/main.tf's Jeffco app_env map add:

~~~hcl
AUTO_ACCEPT_ENABLED = tostring(var.jeffco_auto_accept_enabled)
~~~

In the Jeffco section of infra/terraform.tfvars.example add:

~~~hcl
jeffco_auto_accept_enabled = false
~~~

Do not read or edit the real infra/terraform.tfvars.

- [ ] **Step 5: Validate app and Terraform formatting**

Run:

~~~bash
just test tests/jeffco/test_config.py
just typecheck
terraform fmt -check -recursive infra
~~~

Expected: all pass. If terraform fmt reports only changed task files, run terraform fmt on those explicit files and repeat the check. Do not run just infra-plan because it is authenticated production access.

- [ ] **Step 6: Commit config and infrastructure**

~~~bash
git add apps/jeffco/src/jeffco/config.py apps/jeffco/src/jeffco/main.py tests/jeffco/test_config.py infra/variables.tf infra/main.tf infra/terraform.tfvars.example
git commit -m "Gate Jeffco auto-accept behind configuration"
~~~

---

### Task 7: Documentation and Complete Verification

**Files:**

- Modify: docs/architecture.md
- Modify: docs/invariants.md
- Verify: all files changed by Tasks 1-6

**Interfaces:**

- Documents the exact feature boundary, state behavior, retry behavior, and deployment gate.
- Does not alter runtime interfaces.

- [ ] **Step 1: Update architecture documentation**

In the Jeffco application map, add auto_accept.py as the owner of eligibility, visibility decisions, retries, and outcomes. In the monitor contract/data-flow sections, state that Jeffco performs the mutation inside its fetch implementation before returning app-owned items; the shared runner remains unaware of actions.

Document the sequence:

~~~text
available jobs
  -> existing high-school partition
  -> exact three-school/full-day/seven-day predicate
  -> bounded PUT + assignment verification
  -> outcome-specific key
  -> existing runner diff/render/Discord delivery
~~~

State that disabled mode is the old read-only path.

- [ ] **Step 2: Add explicit invariants**

Add a numbered Jeffco auto-accept section to docs/invariants.md containing:

- Eligibility is exact and fail closed.
- Only the three named normalized schools can trigger a PUT.
- Each continuous visibility episode gets at most one three-attempt sequence.
- Every failed PUT is verified before another mutation.
- Discord never precedes or delays acceptance.
- Ordinary and outcome key namespaces cannot collide.
- Existing single-send-funnel and login-lockout invariants still apply.
- Automatic acceptance remains disabled unless the explicit environment flag is true.

Name these focused tests beside the corresponding invariant:

- test_only_the_three_normalized_target_schools_qualify
- test_other_high_schools_do_not_qualify
- test_three_transient_failures_produce_one_failed_outcome
- test_ambiguous_failure_is_success_when_assignment_is_visible
- test_accepts_a_job_with_the_public_spas_exact_request_shape
- test_outcome_key_is_distinct_from_a_previously_seen_job_id
- test_formats_an_automatic_acceptance_confirmation
- test_auto_accept_is_disabled_by_default

- [ ] **Step 3: Run the standard repository validation**

Run:

~~~bash
just check
~~~

Expected: pytest, mypy strict mode, Ruff lint, and formatting all pass.

- [ ] **Step 4: Run parity and local infrastructure formatting checks**

Run:

~~~bash
just parity
terraform fmt -check -recursive infra
git diff --check
~~~

Expected: the Melanzana and ordinary Jeffco payloads remain identical to their sibling TypeScript repositories; Terraform and Git whitespace checks pass.

Do not run just infra-plan, just infra-verify, just logs, infra/deploy.sh, gcloud, or any image push without separate authorization.

- [ ] **Step 5: Review the final diff against the approved spec**

Confirm:

- exactly three schools appear in the automatic eligibility constant;
- no target-school or lead-time environment override was added;
- all accepted SFE success bodies are ignored;
- error strings contain no server response body;
- the ordinary format_job_alerts function still passes parity;
- the shared runner and Monitor protocol are unchanged;
- the production flag default is false; and
- no real secret-bearing or state file is modified.

- [ ] **Step 6: Commit documentation**

~~~bash
git add docs/architecture.md docs/invariants.md
git commit -m "Document Jeffco auto-accept invariants"
~~~

- [ ] **Step 7: Stop before production activation**

Report the passing verification evidence and the committed implementation. Leave AUTO_ACCEPT_ENABLED false and request separate authorization for each rollout operation described in the spec: deploying the disabled image, then enabling and deploying the live mutation.
