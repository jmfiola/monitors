"""SFE: the pure functions (token handling, the availability-window filter,
response shape validation) and the authenticated client that uses them.

The pure half comes first and has no HTTP in it. `SfeClient` at the bottom is
the login handshake, the token lifecycle, and the retry-on-401 -- it is the only
part that needs a transport, and it takes an `httpx.AsyncClient` rather than
building one so the tests can drive it through `httpx.MockTransport`.

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
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx

from jeffco.types import Job

SFE_BASE = "https://jeffco.eschoolsolutions.com"

#: SFE has no per-job deep link, so alerts point at the list.
AVAILABLE_JOBS_URL = f"{SFE_BASE}/ui/#/substitute/jobs/available"

#: Re-authenticate this far ahead of `exp` rather than waiting to eat a 401.
TOKEN_REFRESH_MARGIN_SEC = 120

#: After this many consecutive login failures the client stops trying for
#: LOGIN_BACKOFF_SEC. This is not politeness -- SFE plausibly locks an account
#: after repeated failures, and the account belongs to a working substitute. A
#: wrong PIN retried on the poll cadence is ~1920 attempts a day, which could
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
    """The login handshake did not end with a token in hand.

    Covers "the landing page had no token" and every guard in the handshake that
    refuses to continue -- a method-preserving redirect, an off-origin or
    unparseable `Location`, a redirect chain, and the lockout suppression. They
    are one type because the caller's response to all of them is the same: fail
    this tick and back off.
    """


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


#: 301/302/303 become a GET by spec, which is what SFE's login flow sends.
REDIRECT_TO_GET = frozenset({301, 302, 303})

#: 307/308 preserve the method and body. Following one here would re-POST the PIN
#: to wherever `Location` points, so it is an error rather than a silent GET.
REDIRECT_PRESERVING_METHOD = frozenset({307, 308})


def _assert_not_html(text: str, content_type: str | None, label: str, status: int) -> None:
    """Refuse an HTML body. An Imperva challenge or a Tomcat error page can
    arrive with a 200 and must never be parsed as data.

    The body is never quoted, not even a short slice: SFE's error pages carry a
    live `;jsessionid=` in the form action, so a slice of one is a working
    session credential written into the logs. The status and the length are
    enough to tell a challenge from an error page.
    """
    looks_html = "text/html" in (content_type or "") or text.lstrip().startswith("<")
    if not looks_html:
        return
    raise SfeShapeError(
        f"SFE {label} returned HTML, not JSON (HTTP {status}, {len(text)} bytes; "
        f"body withheld — SFE error pages embed a session id)"
    )


def _api_message(text: str) -> str:
    """The API's own `message` field, if the body is JSON and has one.

    Deliberately not a slice of the raw body: the message is the part specific
    enough to debug from ("Start date must be in the future."), and the rest is
    the part that might be a credential.
    """
    try:
        parsed = json.loads(text)
    except ValueError:
        # Not JSON. Say nothing about the body beyond its size.
        return f" ({len(text)} bytes, body withheld)"
    message = parsed.get("message") if isinstance(parsed, dict) else None
    if isinstance(message, str) and message != "":
        return f" — {message[:200]}"
    return f" ({len(text)} bytes, body withheld)"


class SfeClient:
    """The authenticated SFE client: log in, hold the token, call the API.

    A class rather than the TypeScript's closure-returning factory. The closure
    existed to hold the cookie jar, the token, its expiry, and the two lockout
    counters; `httpx.AsyncClient` supplies the jar, and a class holds the rest
    more legibly than four `nonlocal`s would.

    The `httpx.AsyncClient` is injected rather than built here so the tests can
    hand it an `httpx.MockTransport`. Every request this class makes passes
    `follow_redirects=False` explicitly, so it does not matter how the injected
    client was configured -- see `_post_following_one_redirect` for why the
    single hop is done by hand.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        user_id: str,
        pin: str,
        timezone: str,
        window_days: int,
        now_unix: Callable[[], int],
        log: Callable[[str], None],
    ) -> None:
        self._client = client
        self._user_id = user_id
        self._pin = pin
        self._timezone = timezone
        self._window_days = window_days
        self._now_unix = now_unix
        self._log = log
        self._token: str | None = None
        self._expires_at_unix = 0
        self._login_failures = 0
        self._login_blocked_until_unix = 0

    # --- the login handshake -------------------------------------------------

    async def _post_following_one_redirect(self, url: str, data: dict[str, str]) -> httpx.Response:
        """Follow exactly one redirect by hand.

        Not cookie plumbing -- httpx's jar handles that. Two security properties
        live here instead. A 307/308 must be *refused* rather than followed,
        because those preserve the method and body and would re-POST the PIN to
        wherever `Location` points. And an off-origin `Location` must be refused,
        because a browser would never carry SFE's cookies cross-origin and
        because a foreign page could plant its own `var token = 'Bearer ...'` for
        `extract_token` to pick up.

        One hop, not a bounded chain. The verified flow is a single 302, and a
        chain would mean either a changed login flow or an Imperva challenge --
        both of which should surface as an error rather than be quietly walked.
        (A bounded loop is also easy to get subtly wrong: it is tempting to
        return only inside the loop, which raises on the last hop even when that
        hop carried the token.)

        Status-only messages throughout: SFE's own `Location` headers
        legitimately carry `;jsessionid=`, so neither the header nor the host it
        names is ever quoted.
        """
        first = await self._send(
            "POST",
            url,
            "login",
            data=data,
            headers={**BROWSER_HEADERS, "content-type": "application/x-www-form-urlencoded"},
        )

        if first.status_code in REDIRECT_PRESERVING_METHOD:
            raise SfeLoginError(
                f"SFE login: HTTP {first.status_code} would re-send the credentials; refusing"
            )
        if first.status_code not in REDIRECT_TO_GET:
            return first

        location = first.headers.get("location")
        if not location:
            raise SfeLoginError(f"SFE login: HTTP {first.status_code} with no location header")

        # Belt and braces with `_send`, which normally reports a malformed header
        # first because httpx parses `Location` while building the `next_request`
        # it hands back. `urlparse` raises ValueError on a malformed
        # authority; its own message does not quote the input today, but this
        # Location may carry a live `;jsessionid=`, so the parser's error is dropped
        # entirely (`from None`) rather than chained -- which keeps a foreign error
        # object from riding along to whatever eventually logs the exception.
        try:
            target = urljoin(url, location)
            target_origin = urlparse(target)
        except ValueError:
            raise SfeLoginError(
                f"SFE login: HTTP {first.status_code} with an unparseable location header"
            ) from None

        # The scheme is part of the comparison, not just the host: a downgrade to
        # http:// is a different origin and would put the session cookies on the
        # wire in the clear.
        expected = urlparse(SFE_BASE)
        if (target_origin.scheme, target_origin.netloc) != (expected.scheme, expected.netloc):
            raise SfeLoginError(
                f"SFE login: refusing to follow a redirect off-origin (HTTP {first.status_code})"
            )

        second = await self._send("GET", target, "login", headers=BROWSER_HEADERS)
        if second.status_code in REDIRECT_TO_GET | REDIRECT_PRESERVING_METHOD:
            raise SfeLoginError(
                f"SFE login: unexpected second redirect (HTTP {second.status_code})"
            )
        return second

    async def _send(
        self,
        method: str,
        url: str,
        label: str,
        *,
        headers: dict[str, str],
        data: dict[str, str] | None = None,
        json_body: object | None = None,
    ) -> httpx.Response:
        """**The only place `self._client` is touched.** Every request in this
        class goes through here, and a new call site that does not is a bug: the
        guard below is the module's boundary against httpx's own error text.

        `follow_redirects=False` stops httpx *sending* the next hop, but it still
        parses `Location` in order to populate `response.next_request` -- and on a
        malformed header that raises `httpx.RemoteProtocolError` whose message
        quotes the offending value ("Invalid port: '1;jsessionid=...'"). SFE's
        `Location` headers legitimately carry `;jsessionid=`, so that message is a
        live session credential. Every response can carry a `Location`, not just
        the login POST -- the init GET and any API response can too -- which is
        why this is a single funnel rather than a guard at the redirect helper.

        `from None` drops httpx's exception rather than chaining it, so nothing
        that prints or logs this error can recover the header from `__context__`.

        `SfeShapeError` rather than `SfeLoginError` for every caller: the fault is
        "the peer did not send a readable HTTP response", which is the same fault
        on the login GET as on an API response and is not a decision the handshake
        made. Uniform, so the funnel cannot be misused. A failure here still
        counts toward the login lockout, because `_login` counts every exception
        the handshake raises. The wording covers both causes, which are
        indistinguishable without matching on httpx's prose: a malformed
        `Location`, and a peer that broke the protocol outright.
        """
        try:
            return await self._client.request(
                method,
                url,
                data=data,
                json=json_body,
                headers=headers,
                follow_redirects=False,
            )
        except httpx.RemoteProtocolError:
            raise SfeShapeError(
                f"SFE {label}: the response could not be read — an unparseable location "
                f"header or a malformed HTTP response (details withheld: SFE's location "
                f"headers carry a session id)"
            ) from None

    async def _handshake(self) -> None:
        """The three-request handshake.

        The init response's body is deliberately not parsed: some responses embed
        a login form whose action carries a `;jsessionid=` path parameter and some
        do not, and login succeeds either way, because the session rides on the
        cookies httpx has just banked. Discarding the body removes the only
        fragile HTML dependency in the flow.
        """
        # Through `_send` like every other request: SFE can answer this GET with a
        # redirect too, and its `Location` carries a session id.
        await self._send("GET", f"{SFE_BASE}/logOnInitAction.do", "login", headers=BROWSER_HEADERS)

        landing = await self._post_following_one_redirect(
            f"{SFE_BASE}/logOnAction.do",
            {"userID": self._user_id, "userPin": self._pin, "bootstrapDevice": ""},
        )

        # extract_token raises when the body holds no token -- see its comment.
        fresh = extract_token(landing.text)
        # Both fields or neither: token_expiry_unix raises on a malformed token,
        # and assigning `self._token` before it runs would leave a token paired
        # with a stale expiry -- which either re-logs in on every single call or,
        # worse, trusts an expiry that has already passed.
        fresh_expiry = token_expiry_unix(fresh)
        self._token = fresh
        self._expires_at_unix = fresh_expiry

        remaining = self._expires_at_unix - self._now_unix()
        self._log(f"authenticated; token valid for {remaining}s")
        if remaining < TOKEN_REFRESH_MARGIN_SEC:
            self._log(
                f"warning: fresh token is already inside the {TOKEN_REFRESH_MARGIN_SEC}s refresh "
                f"margin — every API call will re-authenticate; check this container's clock"
            )

    async def _login(self) -> None:
        """The lockout wrapper. Three consecutive failures, then no login attempt
        for an hour -- see MAX_LOGIN_FAILURES. The suppressed path makes no
        network request at all, which is the whole point of it.
        """
        now = self._now_unix()
        if now < self._login_blocked_until_unix:
            raise SfeLoginError(
                f"SFE login suppressed after {self._login_failures} consecutive login failures; "
                f"not retrying for another {self._login_blocked_until_unix - now}s "
                f"(protecting the account from lockout)"
            )
        try:
            await self._handshake()
        except Exception:
            self._login_failures += 1
            if self._login_failures >= MAX_LOGIN_FAILURES:
                self._login_blocked_until_unix = self._now_unix() + LOGIN_BACKOFF_SEC
                self._log(
                    f"login failed {self._login_failures}x; pausing login attempts for "
                    f"{LOGIN_BACKOFF_SEC}s so a bad credential cannot lock the account"
                )
            raise
        # Only a completed handshake clears the counter, so the ceiling counts
        # *consecutive* failures rather than a lifetime total.
        self._login_failures = 0

    async def _ensure_token(self) -> None:
        if (
            self._token is not None
            and self._now_unix() < self._expires_at_unix - TOKEN_REFRESH_MARGIN_SEC
        ):
            return
        await self._login()

    # --- authenticated requests ---------------------------------------------

    async def _api_request(
        self,
        path: str,
        method: str,
        json_body: object | None,
        label: str,
        allow_retry: bool = True,
    ) -> object:
        """One authenticated API request, with exactly one re-login and retry on
        401. A second 401 raises, so a credential problem fails the tick and
        enters backoff instead of becoming a login hammer.
        """
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
            return await self._api_request(path, method, json_body, label, allow_retry=False)

        # Before the status check, so an HTML error page is reported as HTML
        # rather than having its markup mined for a message.
        _assert_not_html(text, response.headers.get("content-type"), label, response.status_code)

        if not response.is_success:
            raise SfeHttpError(
                response.status_code,
                f"SFE {label} failed: HTTP {response.status_code}{_api_message(text)}",
            )

        try:
            parsed: object = json.loads(text)
        except ValueError as exc:
            raise SfeShapeError(
                f"SFE {label} returned unparseable JSON ({len(text)} bytes)"
            ) from exc
        return parsed

    async def fetch_available_jobs(self) -> list[Job]:
        """The available-jobs list. The filter is rebuilt from `now_unix()` on
        every call, which is what handles midnight rollover and keeps `jobStart`
        out of the past.
        """
        body = await self._api_request(
            "/api/job/available",
            "POST",
            build_available_filter(self._now_unix(), self._window_days, self._timezone),
            "available jobs",
        )
        jobs = parse_jobs(body)
        # A partial drop is otherwise completely silent. `parse_jobs` tolerates a
        # malformed row on purpose so one bad row cannot lose a whole poll, and it
        # raises when *every* row fails -- but in between, a job with (say) a null
        # jobEnd is never announced, never logged, and cannot even reach the
        # heartbeat's gap report, which only covers rows that parsed. Every signal
        # would say healthy while a real job went unmentioned. The count is the only
        # place that difference is visible, so say it out loud.
        if isinstance(body, list) and len(jobs) < len(body):
            self._log(
                f"warning: dropped {len(body) - len(jobs)} of {len(body)} available-jobs "
                "row(s) that failed field validation; those jobs cannot be announced"
            )
        return jobs

    async def fetch_job_detail(self, job_id: int) -> object:
        """Detail for one job. Verified to return 200 even for a job already
        filled, so a job claimed between the list fetch and this call still
        renders a complete alert rather than failing.
        """
        return await self._api_request(f"/api/job/{job_id}", "GET", None, f"job detail {job_id}")
