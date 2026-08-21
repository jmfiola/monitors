"""SFE's pure functions: token handling, the availability-window filter, and
response shape validation. No HTTP here -- the client (login handshake, cookie
jar, retry-on-401) is `jeffco.sfe_client` (Task 6).

Four exception types rather than one bare `Exception`, because callers
discriminate on them: the runner's retry policy turns on `SfeHttpError.status`,
and `pytest.raises(Exception)` would assert nothing.

No secret in any message. SFE's error pages embed a live `;jsessionid=`, the
PIN closely resembles the user id, and the JWT is a bearer credential -- every
error below carries at most a status and a byte count, never a body, a URL, or
a token.
"""

from __future__ import annotations

import base64
import json
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from jeffco.types import Job

SFE_BASE = "https://jeffco.eschoolsolutions.com"

#: SFE has no per-job deep link, so alerts point at the list.
AVAILABLE_JOBS_URL = f"{SFE_BASE}/ui/#/substitute/jobs/available"

#: Re-authenticate this far ahead of `exp` rather than waiting to eat a 401.
TOKEN_REFRESH_MARGIN_SEC = 120

#: After this many consecutive login failures the client stops trying for
#: LOGIN_BACKOFF_SEC. This is not politeness -- SFE plausibly locks an account
#: after repeated failures, and the account belongs to a working substitute. A
#: wrong PIN retried on the poll cadence is ~1440 attempts a day, which could
#: cost dad the ability to pick up work at all. Three tries, then one attempt
#: an hour; the stall alert is what surfaces it.
MAX_LOGIN_FAILURES = 3
LOGIN_BACKOFF_SEC = 3600

#: The site sits behind Imperva, which lets ordinary requests through but wants
#: to see a browser. A plain client User-Agent is refused, so these headers
#: mirror the verified working request. No headless browser is needed.
BROWSER_HEADERS: dict[str, str] = {
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "accept-language": "en-US,en;q=0.9",
}


class SfeHttpError(Exception):
    """A non-ok HTTP status from the API, carrying the status as a field.

    The caller's retry policy turns on it, and recovering a number by
    re-parsing prose is the kind of thing that breaks the next time the
    message is reworded.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class SfeLoginError(Exception):
    """The login handshake produced no token."""


class SfeTokenError(Exception):
    """The token is not a usable JWT."""


class SfeShapeError(Exception):
    """The response's shape is not what the API contract says."""


def is_account_busy(err: object) -> bool:
    """Whether a failure means "the account is busy" rather than "something is
    wrong".

    The monitor logs in as the substitute it watches for, and SFE answers 400
    when a second session touches the account -- every 400 in production has
    landed between 6am and midnight, none in three nights of overnight
    polling, which is human-presence shaped rather than fault shaped. The next
    tick usually succeeds, so this is a signal to keep the normal cadence, not
    to escalate.

    Narrow on purpose: 400 only. A 5xx, a network error, or an unparseable
    body is a real fault and still earns exponential backoff.
    """
    return isinstance(err, SfeHttpError) and err.status == 400


_TOKEN_PATTERN = re.compile(r"var\s+token\s*=\s*'Bearer\s+([A-Za-z0-9._-]+)'")


def extract_token(html: str) -> str:
    """Pull the bearer token out of the `userInterfaceSelectorAction.do` body,
    where SFE plants it in a plain script assignment for its own SPA to pick up.

    Absence of a token is how login failure is detected -- deliberately not a
    credential-specific error signature. That response was never probed,
    because SFE plausibly locks an account after repeated failures and this
    account belongs to a working substitute. Absence-of-token catches wrong
    credentials, an Imperva challenge, and a changed login flow with the same
    raise.

    The body is never included in the error: it may hold a session id.
    """
    match = _TOKEN_PATTERN.search(html)
    if match is None:
        raise SfeLoginError(
            f"SFE login failed: no bearer token in the response body ({len(html)} bytes)"
        )
    return match.group(1)


def token_expiry_unix(jwt: str) -> int:
    """Read `exp` from the JWT. The observed lifetime is 7200s, but reading the
    claim means a district-side change to it needs no code change here.

    No signature verification: we are the token's audience, not its
    validator, and it arrived over TLS from the issuer. Error messages never
    include the token.
    """
    segments = jwt.split(".")
    if len(segments) != 3:
        raise SfeTokenError("SFE token is not a JWT (expected 3 segments)")

    payload_segment = segments[1]
    # JavaScript's `Buffer.from(seg, 'base64url')` tolerates missing padding;
    # Python's decoder does not, and a JWT payload segment very often needs it.
    padded = payload_segment + "=" * (-len(payload_segment) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:
        raise SfeTokenError("SFE token payload is not valid JSON") from exc

    exp = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(exp, bool) or not isinstance(exp, int | float) or not math.isfinite(exp):
        raise SfeTokenError("SFE token has no usable exp claim")
    return int(exp)


def zoned_wall_clock(unix_sec: int, timezone: str, time: str) -> str:
    """ "2026-03-08T00:00:00-07:00" -- the given wall-clock `time` on the local
    date at `unix_sec`, stamped with the offset in effect **at that wall-clock
    time**.

    The TypeScript original needs two passes here: `Intl` can only *format* an
    offset, so it renders once with the offset at `unix_sec`, parses to find
    roughly which instant is meant, then re-renders with the offset actually
    in effect there. On a spring-forward day the offset at noon (-06:00) is
    not the offset at midnight (-07:00), and stamping the former onto the
    latter names an instant on the previous local day -- which the API rejects
    as a past `jobStart`, blinding the monitor for that whole day.

    `zoneinfo` resolves the offset for a local time directly, so the
    correction is structural rather than iterative.
    """
    zone = ZoneInfo(timezone)
    local_date = datetime.fromtimestamp(unix_sec, zone).date()
    hour, minute, second = (int(part) for part in time.split(":"))
    stamped = datetime(
        local_date.year, local_date.month, local_date.day, hour, minute, second, tzinfo=zone
    )
    return stamped.isoformat()


def build_available_filter(now_unix: int, window_days: int, timezone: str) -> dict[str, object]:
    """Build the `POST /api/job/available` body. Every quirk here was learned
    from the API's own validation errors:

    - Timestamps must carry an offset; a bare date is rejected.
    - `jobStart` must not be in the past, but today's local midnight is
      accepted -- so the window is recomputed every tick, which also handles
      midnight rollover.
    - `[0]` means "no filter" for the three id lists. Zero is the sentinel; an
      empty array is not accepted.

    Each end of the window carries the offset in effect at that end, so a
    window spanning a DST change is correct at both ends rather than an hour
    off at one.
    """
    end_unix = now_unix + window_days * 86400
    return {
        "filterOption": {
            "jobStart": zoned_wall_clock(now_unix, timezone, "00:00:00"),
            "jobEnd": zoned_wall_clock(end_unix, timezone, "23:59:59"),
            "locationIdList": [0],
            "locationGroupIdList": [0],
            "classificationIdList": [0],
            "teacher": None,
            "jobId": "",
            "jobInstruction": ["NONE"],
        },
        "paginationOption": {"pageNumber": 1, "pageSize": 500},
    }


def _parse_job_row(entry: object) -> Job | None:
    if not isinstance(entry, dict):
        return None
    job_id = entry.get("jobId")
    location_name = entry.get("locationName")
    job_start = entry.get("jobStart")
    job_end = entry.get("jobEnd")
    if (
        not isinstance(job_id, int)
        or isinstance(job_id, bool)
        or not isinstance(location_name, str)
        or not isinstance(job_start, str)
        or not isinstance(job_end, str)
    ):
        return None

    classf_name = entry.get("classfName")
    employee_first_name = entry.get("employeeFirstName")
    employee_last_name = entry.get("employeeLastName")
    days_of_week = entry.get("daysOfWeek")
    duration_type = entry.get("durationType")
    job_status = entry.get("jobStatus")
    return Job(
        job_id=job_id,
        location_name=location_name,
        job_start=job_start,
        job_end=job_end,
        classf_name=classf_name if isinstance(classf_name, str) else None,
        employee_first_name=employee_first_name if isinstance(employee_first_name, str) else None,
        employee_last_name=employee_last_name if isinstance(employee_last_name, str) else None,
        days_of_week=days_of_week if isinstance(days_of_week, str) else None,
        duration_type=duration_type if isinstance(duration_type, str) else None,
        job_status=job_status if isinstance(job_status, str) else None,
    )


def parse_jobs(body: object) -> list[Job]:
    """Narrow the raw array to jobs the rest of the pipeline can safely use.

    Malformed rows are dropped rather than raised on: a shape change upstream
    should quietly reduce coverage, not post garbage -- and, more importantly,
    not raise. `partition_jobs` calls `.upper()` on `location_name` and the
    date code parses `job_start`, so one row missing a field would raise an
    exception that loses the entire poll, good rows included.

    A single unparseable row is tolerated for that reason -- upstream noise
    should quietly reduce coverage. Every row failing is a different thing: it
    is a shape change, and swallowing it would leave the monitor permanently
    blind while still reporting success.
    """
    if not isinstance(body, list):
        raise SfeShapeError("SFE available-jobs response was not a JSON array")

    jobs = [job for entry in body if (job := _parse_job_row(entry)) is not None]

    if len(body) > 0 and len(jobs) == 0:
        raise SfeShapeError(f"SFE available-jobs: all {len(body)} row(s) failed field validation")

    return jobs
