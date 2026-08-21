"""Ports the `describe('createSfeClient', ...)` half of jeffco-sub-monitor's
test/sfe.test.ts -- the 20 cases that drive the login handshake and the
authenticated API call. The other 20 (the pure functions) are test_sfe_pure.py.

Two translation notes:

- The TypeScript's `fakeFetch` recorded a `redirect: 'manual'` per call and
  asserted every request carried it, because `fetch` follows redirects by
  default and `undici` has no cookie jar. `httpx.MockTransport` cannot see that
  flag -- it is resolved by the client, not carried on the request -- so the
  equivalent assertion is the last test in this file: a client constructed with
  `follow_redirects=True` must still refuse the off-origin hop, which is only
  true if every call site passes `follow_redirects=False`.
- `httpx.AsyncClient` owns the cookie jar, so the hand-rolled `Map` the
  TypeScript needed is gone. The replay test below pins the behaviour anyway:
  it is the jar's *effect* the handshake depends on, not its implementation.

`Harness.clock` is a one-element list so a test can advance time by assignment
without sleeping and without patching a module global.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import traceback
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
from jeffco.sfe import (
    SFE_BASE,
    SfeClient,
    SfeHttpError,
    SfeLoginError,
    SfeShapeError,
    extract_token,
)

FIXTURES = Path(__file__).parent / "fixtures"
LOGIN_HTML = (FIXTURES / "login-page.html").read_text(encoding="utf-8")
AVAILABLE_JOBS = json.loads((FIXTURES / "available-jobs.json").read_text(encoding="utf-8"))
JOB_DETAIL = json.loads((FIXTURES / "job-detail-multiday.json").read_text(encoding="utf-8"))

#: The sanitized fixture token's own exp claim.
FIXTURE_EXP = 1786742381

# The same placeholder credentials sfe.test.ts used. The id is a secret too: the
# PIN is the id plus two characters, so leaking the id leaks most of the PIN.
USER_ID = "900001"
PIN = "900001ZZ"
DENVER = "America/Denver"

#: A `Location` httpx's own URL parser rejects, carrying a sentinel session id.
#: Held in constants so no traceback frame's source line can quote the sentinel
#: and hand the leak checks below a false pass.
SESSION_SENTINEL = "A1B2LIVE_SECRET"
MALFORMED_LOCATION = f"http://[::1;jsessionid={SESSION_SENTINEL}"


def assert_no_leak(exc: BaseException) -> None:
    """Every channel a caught exception can reach, checked for the session id.

    The traceback matters as much as the message: a chained
    `httpx.RemoteProtocolError` quotes the offending header, and
    `traceback.format_exception` is what the poll loop's error logging renders.
    So `__cause__` must be unset and the context suppressed, not merely absent
    from `str(exc)`.
    """
    rendered = "\n".join([str(exc), repr(exc), *traceback.format_exception(exc)])
    assert SESSION_SENTINEL not in rendered
    assert "jsessionid" not in rendered
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


def html(body: str, status: int = 200, cookies: Sequence[str] = ()) -> httpx.Response:
    headers = [("content-type", "text/html;charset=UTF-8")]
    headers.extend(("set-cookie", cookie) for cookie in cookies)
    return httpx.Response(status, headers=headers, text=body)


def json_response(body: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, headers=[("content-type", "application/json")], text=json.dumps(body)
    )


def redirect(location: str, cookies: Sequence[str] = (), status: int = 302) -> httpx.Response:
    headers = [("location", location)]
    headers.extend(("set-cookie", cookie) for cookie in cookies)
    return httpx.Response(status, headers=headers)


def login_responses() -> list[httpx.Response]:
    """The three responses a successful login consumes, in order."""
    return [
        html("<html>whatever</html>"),  # GET /logOnInitAction.do -- body is not parsed
        redirect("/userInterfaceSelectorAction.do", ["AWSALB=hop2; Path=/"]),
        html(LOGIN_HTML),  # the redirect target, which carries the token
    ]


def failed_login() -> list[httpx.Response]:
    """The same three hops, but the landing page has no token -- which is how
    every login failure looks, wrong PIN included. The bad-credential response
    was deliberately never probed."""
    return [
        html("<html></html>"),
        redirect("/userInterfaceSelectorAction.do"),
        html('<html><form id="loginForm"></form></html>'),
    ]


@dataclass(frozen=True)
class Call:
    method: str
    url: str
    headers: dict[str, str]
    body: str


class Harness:
    """Plays back a queue of responses and records every request.

    Deliberately not a URL-keyed map: the order of the auth handshake is part of
    what these tests pin down. An exhausted queue raises rather than returning a
    default, so a test that expects *no* network call fails loudly if one is made.
    """

    def __init__(
        self, responses: Sequence[httpx.Response], now: int, follow_redirects: bool
    ) -> None:
        self._queue = list(responses)
        self.calls: list[Call] = []
        self.logs: list[str] = []
        self.clock = [now]
        self.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle), follow_redirects=follow_redirects
        )
        self.sfe = SfeClient(
            client=self.http,
            user_id=USER_ID,
            pin=PIN,
            timezone=DENVER,
            window_days=180,
            now_unix=lambda: self.clock[0],
            log=self.logs.append,
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(
            Call(
                method=request.method,
                url=str(request.url),
                headers=dict(request.headers),
                body=request.content.decode(),
            )
        )
        if not self._queue:
            raise AssertionError(f"no response queued for {request.method} {request.url}")
        return self._queue.pop(0)

    @property
    def logins(self) -> int:
        return sum(1 for call in self.calls if "logOnAction" in call.url)

    def logged(self, pattern: str) -> bool:
        return any(re.search(pattern, line, re.IGNORECASE) for line in self.logs)


@asynccontextmanager
async def client(
    responses: Sequence[httpx.Response],
    *,
    now: int = FIXTURE_EXP - 3600,  # an hour of token life left
    follow_redirects: bool = False,
) -> AsyncIterator[Harness]:
    harness = Harness(responses, now, follow_redirects)
    try:
        yield harness
    finally:
        await harness.http.aclose()


# --- the happy path ----------------------------------------------------------


async def test_performs_the_exact_handshake_the_live_system_accepted() -> None:
    # One test for the whole happy path rather than three that each replay the
    # same four requests to assert one field. Every value here was observed
    # working against the live API; none of it is inferred.
    async with client([*login_responses(), json_response(AVAILABLE_JOBS)]) as h:
        await h.sfe.fetch_available_jobs()

    assert [f"{c.method} {c.url.replace(SFE_BASE, '')}" for c in h.calls] == [
        "GET /logOnInitAction.do",
        "POST /logOnAction.do",
        "GET /userInterfaceSelectorAction.do",  # the 302 target, followed by hand
        "POST /api/job/available",
    ]

    login = h.calls[1]
    assert login.headers["content-type"] == "application/x-www-form-urlencoded"
    form = dict(parse_qsl(login.body, keep_blank_values=True))
    assert form["userID"] == USER_ID
    assert form["userPin"] == PIN
    assert form["bootstrapDevice"] == ""

    api = h.calls[-1]
    assert api.headers["authorization"].startswith("Bearer eyJ")
    # The TypeScript set this header by hand and the API requires it; httpx sets
    # it from `json=`, so it is asserted rather than assumed.
    assert api.headers["content-type"] == "application/json"
    assert json.loads(api.body)["paginationOption"] == {"pageNumber": 1, "pageSize": 500}

    # Beyond sfe.test.ts, but the reason the flow works at all: Imperva refuses a
    # plain client User-Agent, and httpx supplies its own unless overridden.
    assert all("Chrome/131" in c.headers["user-agent"] for c in h.calls)


async def test_replays_cookies_gathered_across_the_handshake_onto_the_api_call() -> None:
    responses = login_responses()
    # Cookies arrive on the init hop and again on the 302.
    responses[0] = html("<html></html>", cookies=["AWSALB=init; Path=/", "incap_ses_1=abc; Path=/"])
    async with client([*responses, json_response(AVAILABLE_JOBS)]) as h:
        await h.sfe.fetch_available_jobs()

    cookie = h.calls[-1].headers["cookie"]
    # AWSALB was reset on the 302 hop -- the later value must win.
    assert "AWSALB=hop2" in cookie
    assert "incap_ses_1=abc" in cookie
    assert "AWSALB=init" not in cookie


async def test_a_partially_dropped_response_says_so_instead_of_alerting_less_quietly() -> None:
    # parse_jobs tolerates a malformed row on purpose, so one bad row cannot lose a
    # whole poll, and it raises when EVERY row fails. In between, the drop would be
    # invisible without the warning this pins: a job with a null jobEnd is never
    # announced, never logged, and cannot reach the heartbeat's gap report either,
    # because that only covers rows that parsed. Every signal reads healthy while a
    # real job goes unmentioned -- and a missed job costs a real person a day's work.
    good = dict(AVAILABLE_JOBS[0])
    broken = {**good, "jobId": 999999, "jobEnd": None}
    async with client([*login_responses(), json_response([good, broken])]) as h:
        jobs = await h.sfe.fetch_available_jobs()

    assert len(jobs) == 1  # the good row still gets through
    assert h.logged(r"dropped 1 of 2")
    assert h.logged(r"cannot be announced")


async def test_a_fully_parsed_response_logs_no_drop_warning() -> None:
    # The other half: the warning must not cry wolf on a clean response, or it becomes
    # noise that gets filtered out and stops being read.
    async with client([*login_responses(), json_response(AVAILABLE_JOBS)]) as h:
        await h.sfe.fetch_available_jobs()
    assert not h.logged(r"dropped")


async def test_reuses_the_token_across_calls_instead_of_logging_in_every_time() -> None:
    async with client(
        [*login_responses(), json_response(AVAILABLE_JOBS), json_response(JOB_DETAIL)]
    ) as h:
        await h.sfe.fetch_available_jobs()
        await h.sfe.fetch_job_detail(1025535)
    assert h.logins == 1


async def test_re_authenticates_when_the_token_is_within_the_refresh_margin_of_exp() -> None:
    # 60s of life left is inside the 120s margin, so the second call re-logs in
    # pre-emptively rather than waiting to eat a 401.
    async with client(
        [
            *login_responses(),
            json_response(AVAILABLE_JOBS),
            *login_responses(),
            json_response(AVAILABLE_JOBS),
        ]
    ) as h:
        await h.sfe.fetch_available_jobs()
        h.clock[0] = FIXTURE_EXP - 60
        await h.sfe.fetch_available_jobs()
    assert h.logins == 2


async def test_warns_when_a_fresh_token_is_already_inside_the_refresh_margin() -> None:
    # Not in sfe.test.ts; the brief's table asks for it. Without this warning a
    # skewed container clock presents as "every call re-authenticates" with no
    # explanation anywhere in the logs.
    async with client(
        [*login_responses(), json_response(AVAILABLE_JOBS)], now=FIXTURE_EXP - 60
    ) as h:
        await h.sfe.fetch_available_jobs()
    assert h.logged(r"already inside the 120s refresh margin")
    assert h.logged(r"check this container's clock")


# --- the 401 path ------------------------------------------------------------


async def test_re_logs_in_once_and_retries_after_a_401() -> None:
    async with client(
        [
            *login_responses(),
            json_response({"message": "unauthorized"}, 401),
            *login_responses(),
            json_response(AVAILABLE_JOBS),
        ]
    ) as h:
        jobs = await h.sfe.fetch_available_jobs()
    assert len(jobs) == 5
    assert sum(1 for c in h.calls if "/api/job/available" in c.url) == 2


async def test_fails_the_tick_on_a_second_401_rather_than_looping() -> None:
    async with client(
        [
            *login_responses(),
            json_response({"message": "unauthorized"}, 401),
            *login_responses(),
            json_response({"message": "unauthorized"}, 401),
        ]
    ) as h:
        with pytest.raises(SfeHttpError, match="401"):
            await h.sfe.fetch_available_jobs()
    # Exactly two logins: the initial one and the single retry.
    assert h.logins == 2


# --- response validation -----------------------------------------------------


async def test_rejects_an_html_body_instead_of_parsing_it_as_data() -> None:
    # An Imperva challenge arrives as HTML with a 200.
    async with client([*login_responses(), html("<html><body>Request unsuccessful.")]) as h:
        with pytest.raises(SfeShapeError, match=r"(?i)html"):
            await h.sfe.fetch_available_jobs()


async def test_surfaces_the_api_validation_message_and_nothing_else_from_the_body() -> None:
    # The API's own field messages are specific enough to debug from one log
    # line, so they are surfaced. The rest of the body is not: slicing N bytes of
    # an arbitrary response into an error message is how credentials reach logs.
    body = {"message": "Error - Start date must be in the future.", "jsessionid": "A1B2LIVE"}
    async with client([*login_responses(), json_response(body, 400)]) as h:
        with pytest.raises(SfeHttpError) as exc:
            await h.sfe.fetch_available_jobs()
    assert "Start date must be in the future" in str(exc.value)
    assert "A1B2LIVE" not in str(exc.value)


async def test_tags_a_failure_with_its_http_status_so_the_caller_can_pick_a_retry_policy() -> None:
    async with client([*login_responses(), json_response({"nope": True}, 400)]) as h:
        with pytest.raises(SfeHttpError) as exc:
            await h.sfe.fetch_available_jobs()
    assert exc.value.status == 400


async def test_does_not_put_an_html_error_body_in_the_error_message() -> None:
    # SFE's own error pages embed a live `;jsessionid=` in the form action, so a
    # raw body slice here would write a working session credential to the logs.
    # Checked before the status check, so an HTML 500 is reported as HTML.
    page = '<html><form action="/logOnAction.do;jsessionid=A1B2C3LIVESESSION">'
    async with client([*login_responses(), html(page, 500)]) as h:
        with pytest.raises(SfeShapeError, match=r"(?i)html") as exc:
            await h.sfe.fetch_available_jobs()
    assert "jsessionid" not in str(exc.value)
    assert "A1B2C3LIVESESSION" not in str(exc.value)


async def test_rejects_a_non_json_success_body_naming_only_its_length() -> None:
    # Not in sfe.test.ts; the brief's table asks for it. A 200 whose body is
    # neither HTML nor JSON must not be handed to parse_jobs.
    body = httpx.Response(200, headers=[("content-type", "application/json")], text="a=1&sid=LIVE")
    async with client([*login_responses(), body]) as h:
        with pytest.raises(SfeShapeError, match=r"(?i)unparseable json") as exc:
            await h.sfe.fetch_available_jobs()
    assert "12 bytes" in str(exc.value)
    assert "LIVE" not in str(exc.value)


# --- the login lockout -------------------------------------------------------


async def test_throws_a_clear_error_when_login_yields_no_token() -> None:
    async with client(failed_login()) as h:
        with pytest.raises(SfeLoginError, match=r"(?i)no bearer token"):
            await h.sfe.fetch_available_jobs()


async def test_stops_attempting_to_log_in_after_three_consecutive_failures() -> None:
    # The account belongs to a working substitute and SFE plausibly locks an
    # account after repeated failures. Left alone, a wrong PIN would be retried
    # every 60 seconds -- roughly 1440 attempts a day -- and could cost the
    # account holder the ability to pick up work at all. Three tries, then
    # silence.
    #
    # Only three logins' worth of responses are queued: the fourth attempt must
    # make no network request at all, so if it tried, the harness would raise
    # AssertionError instead of the SfeLoginError asserted below.
    async with client([*failed_login(), *failed_login(), *failed_login()]) as h:
        for _ in range(3):
            with pytest.raises(SfeLoginError):
                await h.sfe.fetch_available_jobs()

        assert h.logins == 3
        assert h.logged(r"pausing login attempts")

        calls_before = len(h.calls)
        with pytest.raises(SfeLoginError, match=r"(?i)suppressed"):
            await h.sfe.fetch_available_jobs()
        # The load-bearing assertion: not merely that it raised, but that it
        # reached the network zero times while raising.
        assert len(h.calls) == calls_before
        assert h.logins == 3


async def test_says_why_it_is_not_trying_so_the_failure_is_diagnosable() -> None:
    async with client([*failed_login(), *failed_login(), *failed_login()]) as h:
        for _ in range(3):
            with pytest.raises(SfeLoginError, match=r"(?i)no bearer token"):
                await h.sfe.fetch_available_jobs()
        with pytest.raises(SfeLoginError, match=r"3 consecutive login failures"):
            await h.sfe.fetch_available_jobs()


async def test_tries_once_more_after_the_cooldown_elapses() -> None:
    # The latch must not be permanent: a district-side blip must heal without
    # someone noticing and restarting the container.
    async with client(
        [
            *failed_login(),
            *failed_login(),
            *failed_login(),
            *login_responses(),
            json_response(AVAILABLE_JOBS),
        ]
    ) as h:
        for _ in range(3):
            with pytest.raises(SfeLoginError):
                await h.sfe.fetch_available_jobs()
        h.clock[0] += 3601
        assert len(await h.sfe.fetch_available_jobs()) == 5
        assert h.logins == 4


async def test_a_success_resets_the_failure_counter() -> None:
    # Not in sfe.test.ts; the brief's table asks for it. Without the reset the
    # counter is a lifetime total, and two failures a week apart either side of
    # hundreds of successes would latch the lockout.
    async with client(
        [
            *failed_login(),
            *failed_login(),
            *login_responses(),
            json_response(AVAILABLE_JOBS),
            *failed_login(),
            *failed_login(),
        ]
    ) as h:
        for _ in range(2):
            with pytest.raises(SfeLoginError):
                await h.sfe.fetch_available_jobs()
        assert len(await h.sfe.fetch_available_jobs()) == 5
        # Inside the refresh margin, so the next two calls each try to log in
        # again rather than reusing the token the success just banked.
        h.clock[0] = FIXTURE_EXP - 60
        for _ in range(2):
            with pytest.raises(SfeLoginError, match=r"(?i)no bearer token"):
                await h.sfe.fetch_available_jobs()
        # Five real login attempts: the counter went back to zero at the success,
        # so the two later failures are #1 and #2, not #3 and #4.
        assert h.logins == 5
        assert not h.logged(r"pausing login attempts")


# --- the redirect guards -----------------------------------------------------


async def test_refuses_a_second_redirect_instead_of_chasing_a_chain() -> None:
    # The verified flow is exactly one hop. Chasing a chain would mean the token
    # page could be several hops away, and a bounded-loop version raises after N
    # hops even when hop N *was* the token page. One hop or an error.
    async with client(
        [html("<html></html>"), redirect("/hop-one.do"), redirect("/hop-two.do")]
    ) as h:
        with pytest.raises(SfeLoginError, match=r"(?i)second redirect"):
            await h.sfe.fetch_available_jobs()


async def test_refuses_a_redirect_that_points_off_origin_and_never_issues_the_request() -> None:
    # Resolving a Location against the request URL yields the *foreign* origin
    # for an absolute Location, so an off-origin hop would otherwise carry the
    # cookie jar to a foreign host. A browser would never send SFE's cookies
    # cross-origin on a redirect; this client must not either. It also blocks a
    # foreign page planting its own `var token = 'Bearer ...'` for extract_token
    # to pick up. The Location carries a live jsessionid, mirroring the real leak
    # shape -- and the message must not name the host either, because SFE's own
    # Location headers legitimately carry `;jsessionid=`.
    async with client(
        [html("<html></html>"), redirect("https://evil.example/steal;jsessionid=A1B2LIVE")]
    ) as h:
        with pytest.raises(SfeLoginError, match=r"(?i)off-origin") as exc:
            await h.sfe.fetch_available_jobs()
        assert "jsessionid" not in str(exc.value)
        assert "A1B2LIVE" not in str(exc.value)
        assert "evil.example" not in str(exc.value)
        # The off-origin hop must never be issued: only the init GET and the POST
        # that carried the bad redirect should have reached the transport.
        assert len(h.calls) == 2


async def test_reports_a_malformed_location_header_on_the_login_post_without_leaking_it() -> None:
    # The raised error must be this module's own, status-only, rather than the
    # URL parser's: httpx quotes the offending value in its RemoteProtocolError
    # ("Invalid port: '1;jsessionid=A1B2LIVE_SECRET'"), and this Location
    # legitimately carries `;jsessionid=` just like SFE's real ones do -- so a
    # bare log of a foreign error object is exactly the leak this guard stops.
    async with client([html("<html></html>"), redirect(MALFORMED_LOCATION)]) as h:
        with pytest.raises(SfeShapeError, match=r"(?i)unparseable location") as exc:
            await h.sfe.fetch_available_jobs()
    assert_no_leak(exc.value)


async def test_reports_a_malformed_location_header_on_the_handshake_get_without_leaking_it() -> (
    None
):
    # The init GET is a redirect target too, and its Location carries a session
    # id like every other SFE Location. This is the path a first version of the
    # guard missed by wrapping only the sends inside the redirect helper: the raw
    # httpx.RemoteProtocolError escaped with the session id in its message AND in
    # the traceback. Every send now goes through SfeClient._send.
    async with client([redirect(MALFORMED_LOCATION)]) as h:
        with pytest.raises(SfeShapeError, match=r"(?i)unparseable location") as exc:
            await h.sfe.fetch_available_jobs()
        assert len(h.calls) == 1  # it failed on the very first hop
    assert_no_leak(exc.value)


async def test_reports_a_malformed_location_header_on_an_api_response_without_leaking_it() -> None:
    # The second path the funnel closes: an authenticated response can carry a
    # Location too (a session timeout answers 302), so the API request needs the
    # same guard as the handshake rather than trusting the status checks below it.
    async with client([*login_responses(), redirect(MALFORMED_LOCATION)]) as h:
        with pytest.raises(SfeShapeError, match=r"(?i)unparseable location") as exc:
            await h.sfe.fetch_available_jobs()
        # Named the API step, not the login, so the log says where it happened.
        assert "available jobs" in str(exc.value)
    assert_no_leak(exc.value)


async def test_reports_a_missing_location_header_with_the_status_only() -> None:
    # Not in sfe.test.ts; the brief's table asks for it alongside the
    # unparseable case.
    async with client([html("<html></html>"), httpx.Response(302)]) as h:
        with pytest.raises(SfeLoginError, match=r"(?i)no location header") as exc:
            await h.sfe.fetch_available_jobs()
    assert "302" in str(exc.value)
    assert len(h.calls) == 2


async def test_refuses_a_307_rather_than_re_sending_the_credentials() -> None:
    # 301/302/303 become a GET by spec; 307/308 preserve the method and body,
    # which here would mean POSTing the PIN to wherever Location points.
    async with client(
        [html("<html></html>"), httpx.Response(307, headers={"location": "/elsewhere.do"})]
    ) as h:
        with pytest.raises(SfeLoginError, match="307") as exc:
            await h.sfe.fetch_available_jobs()
        assert "/elsewhere.do" not in str(exc.value)
        assert len(h.calls) == 2


async def test_manual_redirect_handling_survives_a_client_that_follows_redirects() -> None:
    # The port of sfe.test.ts's `calls.every(c => c.redirect === 'manual')`:
    # MockTransport cannot observe that flag, so this asserts its effect instead.
    # With follow_redirects left to the client, httpx would fetch evil.example
    # itself and hand its body to extract_token; every call site therefore passes
    # follow_redirects=False.
    async with client(
        [html("<html></html>"), redirect("https://evil.example/steal")], follow_redirects=True
    ) as h:
        with pytest.raises(SfeLoginError, match=r"(?i)off-origin"):
            await h.sfe.fetch_available_jobs()
        assert len(h.calls) == 2


# --- job detail --------------------------------------------------------------


async def test_fetches_job_detail_by_id() -> None:
    # Verified against the real service: 200 even for a job already filled, so a
    # job claimed between the list fetch and this call still renders a complete
    # alert rather than failing.
    async with client([*login_responses(), json_response(JOB_DETAIL)]) as h:
        detail = await h.sfe.fetch_job_detail(1025535)
    assert isinstance(detail, dict)
    assert len(detail["jobDetails"]) == 2
    assert h.calls[-1].url == f"{SFE_BASE}/api/job/1025535"
    assert h.calls[-1].method == "GET"


# --- the credential sweep ----------------------------------------------------


async def test_keeps_the_pin_the_access_id_and_the_token_out_of_every_output_channel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The single most important test in this file: everything else here is
    # recoverable. A leaked PIN is a district account, a leaked bearer token is a
    # live session. The access id counts as a secret too -- the PIN is that id
    # plus two characters, so leaking the id leaks most of the PIN.
    #
    # Checks four channels, not one: the injected logger, stdout and stderr (in
    # case something prints directly), and the raised error, which is the channel
    # the poll loop actually writes to disk.
    async with client(
        [*login_responses(), json_response({"message": "Error - something went wrong"}, 400)]
    ) as h:
        with pytest.raises(SfeHttpError) as exc:
            await h.sfe.fetch_available_jobs()

    captured = capsys.readouterr()
    everything = "\n".join([*h.logs, captured.out, captured.err, str(exc.value)])

    for secret in (PIN, USER_ID, extract_token(LOGIN_HTML)):
        assert secret not in everything
    # Guards against the assertions above passing vacuously against an empty
    # string: the run really did produce output, and really did fail.
    assert "something went wrong" in everything


# --- the single-funnel guard --------------------------------------------------


def test_self_client_is_touched_from_exactly_one_place_in_the_source() -> None:
    """Pins `_send`'s own claim: it is the only place `self._client` is touched.

    A send site that calls `self._client.request` directly bypasses `_send`'s guard
    around `httpx.RemoteProtocolError`, which leaks a live `;jsessionid=` through
    the raw exception message (see `_send`'s docstring). That has happened once, so
    the count is pinned rather than trusted. The AST count includes both direct calls
    and aliases such as `client = self._client`; a behavioural test could only catch
    this by contriving every guarded path to raise, which the rest of this file
    already does per-guard, not by construction the way this one does.
    """
    source = Path(inspect.getfile(SfeClient)).read_text(encoding="utf-8")
    call_sites = re.findall(r"await self\._client\.", source)
    assert len(call_sites) == 1
    client_touches = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr == "_client"
    ]
    assert len(client_touches) == 2  # assignment in __init__, use in _send
