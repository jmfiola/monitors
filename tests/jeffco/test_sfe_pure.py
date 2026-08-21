"""Ports the pure-function half of jeffco-sub-monitor's test/sfe.test.ts.

The other half — everything under `describe('createSfeClient', ...)`, which
drives a fake `fetch` through the login handshake and the authenticated API
call — is Task 6. This file's 20 tests are the other 20 of sfe.test.ts's 40.

Timestamps are pinned as computed constants (never `datetime.now()`), using
the same command style Task 4's test_dates.py used:

    python3 -c "
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    UTC = timezone.utc
    DEN = ZoneInfo('America/Denver')
    for label, dt in [
        ('NOON_MDT', datetime(2026, 8, 14, 18, 0, 0, tzinfo=UTC)),
        ('AUG15_0400Z', datetime(2026, 8, 15, 4, 0, 0, tzinfo=UTC)),
        ('MAR8_1800Z', datetime(2026, 3, 8, 18, 0, 0, tzinfo=UTC)),
        ('NOV1_1900Z', datetime(2026, 11, 1, 19, 0, 0, tzinfo=UTC)),
        ('MST_MAR6_1200', datetime(2026, 3, 6, 12, 0, 0, tzinfo=DEN)),
    ]:
        print(f'{label:16} {int(dt.timestamp())}  {dt.astimezone(DEN).isoformat()}')
    "
"""

import base64
import json
from pathlib import Path

import pytest
from jeffco.sfe import (
    SfeHttpError,
    SfeLoginError,
    SfeShapeError,
    SfeTokenError,
    build_available_filter,
    extract_token,
    is_account_busy,
    parse_jobs,
    token_expiry_unix,
)

DENVER = "America/Denver"

FIXTURES = Path(__file__).parent / "fixtures"
LOGIN_HTML = (FIXTURES / "login-page.html").read_text(encoding="utf-8")
AVAILABLE_JOBS = json.loads((FIXTURES / "available-jobs.json").read_text(encoding="utf-8"))

# The fixture token's own `exp` claim (see the JWT payload embedded in login-page.html).
FIXTURE_EXP = 1786742381

# 1786730400 is 2026-08-14T18:00Z -- noon in Denver on the day the live API was
# probed (sfe.test.ts's NOON_MDT), carried over unchanged.
NOON_MDT = 1786730400
# 2026-08-15T04:00Z -- 10 PM MDT on the *14th* in Denver; UTC and Denver disagree
# on the calendar date here.
AUG15_0400Z = 1786766400
# 2026-03-08T18:00Z -- noon Denver on spring-forward day (MDT, -06:00). Local
# midnight that same day was still MST (-07:00).
MAR8_1800Z = 1772992800
# 2026-11-01T19:00Z -- noon Denver on fall-back day (MST, -07:00). Local
# midnight that same day was still MDT (-06:00).
NOV1_1900Z = 1793559600
# 2026-03-06T12:00-07:00 -- noon Denver two days before spring-forward. Used by
# the bonus test below to exercise a short window that spans the transition.
MST_MAR6_1200 = 1772823600


# --- extract_token (sfe.test.ts: describe('extractToken')) ------------------


def test_extract_token_pulls_the_bearer_token_out_of_the_real_login_page() -> None:
    token = extract_token(LOGIN_HTML)
    assert token.startswith("eyJ")
    assert "Bearer" not in token
    assert len(token.split(".")) == 3


def test_extract_token_throws_when_the_body_has_no_token() -> None:
    # This is how login failure is detected -- no credential-specific signature.
    with pytest.raises(SfeLoginError, match=r"(?i)no bearer token"):
        extract_token('<html><form id="login"></form></html>')


def test_extract_token_does_not_leak_the_body_into_the_error_message() -> None:
    # Captured explicitly rather than only matching a pattern, so an
    # implementation that swallowed the failure (returning '' instead of
    # raising) would not vacuously pass this.
    body = '<html><form action="/logOnAction.do;jsessionid=A1B2LIVE">'
    with pytest.raises(SfeLoginError, match=r"(?i)no bearer token") as exc:
        extract_token(body)
    assert "A1B2LIVE" not in str(exc.value)


# --- token_expiry_unix (sfe.test.ts: describe('tokenExpiryUnix')) -----------


def test_token_expiry_unix_reads_exp_from_the_fixture_token() -> None:
    assert token_expiry_unix(extract_token(LOGIN_HTML)) == FIXTURE_EXP


def test_token_expiry_unix_throws_on_a_non_jwt() -> None:
    with pytest.raises(SfeTokenError, match=r"(?i)jwt"):
        token_expiry_unix("not-a-jwt")


def test_token_expiry_unix_throws_when_exp_is_missing() -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "x"}).encode()).rstrip(b"=")
    with pytest.raises(SfeTokenError, match=r"(?i)exp"):
        token_expiry_unix(f"a.{payload.decode()}.c")


def test_token_expiry_unix_never_includes_the_token_in_the_error_message() -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "x"}).encode()).rstrip(b"=")
    with pytest.raises(SfeTokenError, match=r"(?i)exp") as exc:
        token_expiry_unix(f"header.{payload.decode()}.signature")
    assert payload.decode() not in str(exc.value)


# Extra malformed-input and no-usable-exp coverage from the brief's own
# examples, kept alongside the 1:1 ports above rather than replacing them.


def test_token_expiry_unix_rejects_malformed_tokens_without_quoting_them() -> None:
    for bad in ("notajwt", "a.b", "a.!!!.c"):
        with pytest.raises(SfeTokenError) as exc:
            token_expiry_unix(bad)
        assert bad not in str(exc.value)


def test_token_expiry_unix_rejects_a_payload_with_no_usable_exp() -> None:
    for claims in ({}, {"exp": "soon"}, {"exp": None}):
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
        with pytest.raises(SfeTokenError):
            token_expiry_unix(f"h.{payload.decode()}.s")


# --- build_available_filter (sfe.test.ts: describe('buildAvailableFilter')) -


def test_build_available_filter_reproduces_the_exact_payload_the_live_api_accepted() -> None:
    assert build_available_filter(NOON_MDT, 180, DENVER) == {
        "filterOption": {
            "jobStart": "2026-08-14T00:00:00-06:00",
            "jobEnd": "2027-02-10T23:59:59-07:00",
            "locationIdList": [0],
            "locationGroupIdList": [0],
            "classificationIdList": [0],
            "teacher": None,
            "jobId": "",
            "jobInstruction": ["NONE"],
        },
        "paginationOption": {"pageNumber": 1, "pageSize": 500},
    }


def test_build_available_filter_carries_each_end_of_the_window_at_its_own_utc_offset() -> None:
    # The window opens in MDT (-06:00) and closes in MST (-07:00). A single
    # offset applied to both ends would be an hour wrong at one end.
    filter_option = build_available_filter(NOON_MDT, 180, DENVER)["filterOption"]
    assert isinstance(filter_option, dict)
    assert filter_option["jobStart"].endswith("-06:00")
    assert filter_option["jobEnd"].endswith("-07:00")


def _start_of(unix_sec: int) -> str:
    filter_option = build_available_filter(unix_sec, 1, DENVER)["filterOption"]
    assert isinstance(filter_option, dict)
    job_start = filter_option["jobStart"]
    assert isinstance(job_start, str)
    return job_start


def test_build_available_filter_uses_the_denver_date_not_the_utc_date() -> None:
    # The API rejects a past jobStart but accepts today's local midnight -- it
    # compares by date, not by instant. 04:00Z on the 15th is 10 PM MDT on the
    # *14th*, so a UTC-based implementation would skip a day of postings.
    assert _start_of(AUG15_0400Z) == "2026-08-14T00:00:00-06:00"


def test_build_available_filter_stamps_midnight_with_the_offset_in_effect_at_midnight() -> None:
    # 2026-03-08 is spring-forward day. At noon Denver is MDT, but local
    # midnight was still MST. Stamping the noon offset onto midnight would name
    # the *previous* local day, which the API rejects as a past jobStart.
    assert _start_of(MAR8_1800Z) == "2026-03-08T00:00:00-07:00"
    # 2026-11-01 is the mirror case: noon is MST, midnight was MDT.
    assert _start_of(NOV1_1900Z) == "2026-11-01T00:00:00-06:00"


def test_build_available_filter_renders_utc_with_an_explicit_offset() -> None:
    # Only reachable from tests -- production is pinned to Denver.
    filter_option = build_available_filter(NOON_MDT, 1, "UTC")["filterOption"]
    assert isinstance(filter_option, dict)
    assert filter_option["jobStart"] == "2026-08-14T00:00:00+00:00"


def test_build_available_filter_a_short_window_is_correct_at_both_ends_of_a_dst_jump() -> None:
    # Bonus, beyond sfe.test.ts: a 7-day window that itself straddles the
    # spring-forward, rather than only sampling each end from an independent call.
    filter_option = build_available_filter(MST_MAR6_1200, 7, DENVER)["filterOption"]
    assert isinstance(filter_option, dict)
    assert filter_option["jobStart"] == "2026-03-06T00:00:00-07:00"  # MST
    assert filter_option["jobEnd"] == "2026-03-13T23:59:59-06:00"  # MDT, after the change


# --- parse_jobs (sfe.test.ts: describe('parseJobs')) -------------------------


def test_parse_jobs_accepts_the_real_five_job_array() -> None:
    jobs = parse_jobs(AVAILABLE_JOBS)
    assert len(jobs) == 5
    assert 1025535 in [j.job_id for j in jobs]


def test_parse_jobs_returns_an_empty_array_when_there_are_no_jobs() -> None:
    assert parse_jobs([]) == []


def test_parse_jobs_drops_rows_missing_any_field_the_rest_of_the_pipeline_dereferences() -> None:
    # partition_jobs calls .upper() on locationName and the date code parses
    # jobStart: one row missing either field would raise and take out the four
    # good ones alongside it if it were not filtered here instead.
    good = {
        "jobId": 7,
        "locationName": "Y",
        "jobStart": "2026-10-16T13:45Z",
        "jobEnd": "2026-10-16T21:30Z",
    }
    rows = [
        {"locationName": "X", "jobStart": "a", "jobEnd": "b"},  # no jobId
        {"jobId": 8, "jobStart": "a", "jobEnd": "b"},  # no locationName
        {"jobId": 9, "locationName": "Z", "jobEnd": "b"},  # no jobStart
        {"jobId": 10, "locationName": "Z", "jobStart": "a"},  # no jobEnd
        {"jobId": 11, "locationName": 42, "jobStart": "a", "jobEnd": "b"},  # wrong type
        good,
    ]
    jobs = parse_jobs(rows)
    assert len(jobs) == 1
    assert jobs[0].job_id == 7
    assert jobs[0].location_name == "Y"


def test_parse_jobs_throws_when_the_body_is_not_an_array() -> None:
    # Distinct from an empty array on purpose: `[]` means "nothing available", a
    # non-array means the API changed shape, and treating the second as the
    # first would wipe state and re-alert everything on the next good poll.
    with pytest.raises(SfeShapeError, match=r"(?i)array"):
        parse_jobs({"error": "nope"})


def test_parse_jobs_throws_when_every_row_in_a_non_empty_array_fails_validation() -> None:
    # N rows, none parseable, is a shape change -- the same category as the
    # non-array case, not the per-row tolerance that drops one bad row among
    # good ones.
    rows = [
        {"locationName": "X", "jobStart": "a", "jobEnd": "b"},  # no jobId
        {"locationName": "Y", "jobStart": "c", "jobEnd": "d"},  # no jobId
    ]
    with pytest.raises(SfeShapeError, match=r"all 2 row\(s\) failed field validation"):
        parse_jobs(rows)


def test_parse_jobs_still_returns_the_one_good_row_out_of_a_mixed_batch() -> None:
    # Proves the guard above did not over-correct into losing the per-row
    # tolerance: one bad row alongside a good one must still return the good row.
    good = {
        "jobId": 7,
        "locationName": "Y",
        "jobStart": "2026-10-16T13:45Z",
        "jobEnd": "2026-10-16T21:30Z",
    }
    rows = [{"locationName": "X", "jobStart": "a", "jobEnd": "b"}, good]  # no jobId
    jobs = parse_jobs(rows)
    assert len(jobs) == 1
    assert jobs[0].job_id == 7


# --- is_account_busy (sfe.test.ts: describe('isAccountBusy')) ----------------


def test_is_account_busy_recognizes_a_400_as_the_account_holder_being_on_the_site() -> None:
    assert is_account_busy(SfeHttpError(400, "SFE available jobs failed: HTTP 400")) is True


def test_is_account_busy_leaves_every_other_failure_to_exponential_backoff() -> None:
    # The whole point of the narrow check: a real fault still escalates. A 500
    # or a dropped connection is not a session collision, and treating it as
    # one would hammer a struggling service at full cadence.
    assert is_account_busy(SfeHttpError(500, "HTTP 500")) is False
    assert is_account_busy(SfeHttpError(429, "HTTP 429")) is False
    assert is_account_busy(RuntimeError("SFE available jobs failed: HTTP 400")) is False
    assert is_account_busy(TypeError("fetch failed")) is False
    assert is_account_busy(None) is False
