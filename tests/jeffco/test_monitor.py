"""The app-level half of jeffco-sub-monitor's test/index.test.ts.

index.test.ts has 24 cases. The loop half -- first-run suppression, diffing
against the previous key set, post spacing, withholding un-posted keys,
multi-message batches, resolveStatusUrl, and every runLiveness case -- is
library-owned and already covered in tests/lib/test_runner_tick.py,
test_runner_liveness.py, and test_config.py's status_url test. The remaining
9 cases are the ones only JeffcoMonitor can be wrong about, and are ported
below using a real `SfeClient` over `httpx.MockTransport` so the login
handshake stays honest rather than stubbed away.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from jeffco.config import load_config
from jeffco.monitor import JeffcoMonitor
from jeffco.sfe import SfeClient, SfeHttpError
from jeffco.types import Job
from monitor.types import SourceBusy

FIXTURES = Path(__file__).parent / "fixtures"
LOGIN_HTML = (FIXTURES / "login-page.html").read_text(encoding="utf-8")
AVAILABLE_JOBS = json.loads((FIXTURES / "available-jobs.json").read_text(encoding="utf-8"))
MULTIDAY = json.loads((FIXTURES / "job-detail-multiday.json").read_text(encoding="utf-8"))
SINGLE = json.loads((FIXTURES / "job-detail-single.json").read_text(encoding="utf-8"))

WEBHOOK = "https://discord.test/webhook"
USER_ID = "900001"
PIN = "900001ZZ"

#: The sanitized fixture token's own exp claim (see tests/jeffco/test_sfe_client.py).
FIXTURE_EXP = 1786742381
NOW = FIXTURE_EXP - 3600  # an hour of token life left -- one login for the whole test


def _html(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, headers=[("content-type", "text/html;charset=UTF-8")], text=body)


def _json_response(body: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, headers=[("content-type", "application/json")], text=json.dumps(body)
    )


def _redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers=[("location", location)])


def _login_responses() -> list[httpx.Response]:
    return [
        _html("<html>whatever</html>"),  # GET /logOnInitAction.do
        _redirect("/userInterfaceSelectorAction.do"),
        _html(LOGIN_HTML),  # the redirect target, which carries the token
    ]


def _queue(responses: list[httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if not remaining:
            raise AssertionError(f"no response queued for {request.method} {request.url}")
        return remaining.pop(0)

    return handler


def make_monitor(
    responses: list[httpx.Response], logs: list[str] | None = None
) -> tuple[JeffcoMonitor, httpx.AsyncClient]:
    sink = logs if logs is not None else []
    cfg = load_config({"SFE_USER_ID": USER_ID, "SFE_PIN": PIN, "DISCORD_WEBHOOK_URL": WEBHOOK})
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
    monitor = JeffcoMonitor(cfg, sfe, log=sink.append, now_unix=lambda: NOW)
    return monitor, client


def _job(job_id: int, location_name: str) -> dict[str, object]:
    """A minimal available-jobs row -- just the fields partition_jobs reads."""
    return {
        "jobId": job_id,
        "locationName": location_name,
        "jobStart": "2026-10-16T13:45Z",
        "jobEnd": "2026-10-16T21:30Z",
    }


# --- fetch() -------------------------------------------------------------


async def test_fetch_filters_to_hs_and_only_hs_keys_enter_state() -> None:
    # available-jobs.json holds 3 HS jobs (1025548, 1025533, 1025535) and 2 rows
    # at FOSTER DUAL LANGUAGE PK-8, which is not a high school and must never
    # reach the returned list -- a non-HS job entering state would let a later
    # school-list edit resurrect its stale id as "new".
    monitor, client = make_monitor([*_login_responses(), _json_response(AVAILABLE_JOBS)])
    async with client:
        jobs = await monitor.fetch()
    assert sorted(monitor.key(j) for j in jobs) == ["1025533", "1025535", "1025548"]


async def test_fetch_records_gaps_that_reach_the_heartbeat() -> None:
    logs: list[str] = []
    monitor, client = make_monitor([*_login_responses(), _json_response(AVAILABLE_JOBS)], logs)
    async with client:
        await monitor.fetch()
    extras = monitor.heartbeat_extras()
    assert extras.fields is not None
    assert "FOSTER DUAL LANGUAGE PK-8" in extras.fields[0].value
    # Each name is also logged the first time it is seen -- the logs are where
    # this gets noticed within the day.
    assert any("FOSTER DUAL LANGUAGE PK-8" in line for line in logs)


async def test_gaps_accumulate_across_ticks_rather_than_being_replaced() -> None:
    # A school can appear in one poll and be claimed before the next, while the
    # heartbeat fires once a day -- keeping only the latest tick's names would
    # drop exactly what the report exists to surface.
    tick1 = [_job(1, "RALSTON VALLEY HS"), _job(2, "SOME MIDDLE SCHOOL")]
    tick2 = [_job(3, "RALSTON VALLEY HS"), _job(4, "ANOTHER ELEMENTARY")]  # SOME MIDDLE SCHOOL gone
    monitor, client = make_monitor(
        [*_login_responses(), _json_response(tick1), _json_response(tick2)]
    )
    async with client:
        await monitor.fetch()
        await monitor.fetch()
    extras = monitor.heartbeat_extras()
    assert extras.fields is not None
    value = extras.fields[0].value
    assert "SOME MIDDLE SCHOOL" in value
    assert "ANOTHER ELEMENTARY" in value


async def test_a_newly_discovered_school_reaches_a_report_the_old_ones_already_filled() -> None:
    # `heartbeat_extras_for` keeps the last MAX_GAP_NAMES entries, so the order
    # `_unmatched_seen` hands them over decides who a reader ever sees. Insertion
    # order makes that "the most recently discovered"; sorting it would make it
    # "the alphabetically last", and a new campus early in the alphabet would then
    # never appear once ten gaps had accumulated. An earlier draft of this method
    # sorted; this test is what stops it being tidied back.
    #
    # Only the CROSS-TICK order is pinned. Within one tick `partition_jobs` returns
    # a sorted set, and that is fine: every name in one response was discovered at
    # the same instant, so there is no "newer" among them to preserve.
    full = [f"{letter} MIDDLE SCHOOL" for letter in "QRSTUVWXYZ"]  # exactly ten
    monitor, client = make_monitor(
        [
            *_login_responses(),
            _json_response([_job(i, n) for i, n in enumerate(full)]),
            # Sorts first alphabetically, discovered last -- the case that separates
            # insertion order from sorted order.
            _json_response([_job(99, "A BRAND NEW MIDDLE SCHOOL")]),
        ]
    )
    async with client:
        await monitor.fetch()
        await monitor.fetch()
    extras = monitor.heartbeat_extras()
    assert extras.fields is not None
    value = extras.fields[0].value
    assert "A BRAND NEW MIDDLE SCHOOL" in value
    assert "Q MIDDLE SCHOOL" not in value  # pushed out to make room
    assert "…and 1 more" in value


async def test_sfe_400_becomes_source_busy_so_the_runner_holds_cadence() -> None:
    # See jeffco.sfe.is_account_busy: SFE answers 400 while the account holder's
    # own session is active, which is signal to hold cadence, not to escalate.
    monitor, client = make_monitor([*_login_responses(), _json_response({"message": "busy"}, 400)])
    async with client:
        with pytest.raises(SourceBusy):
            await monitor.fetch()


async def test_sfe_500_stays_an_sfehttperror_and_earns_backoff() -> None:
    # A real fault must not be swallowed as SourceBusy -- only a 400 is a busy
    # signal (see jeffco.sfe.is_account_busy); everything else still escalates.
    monitor, client = make_monitor([*_login_responses(), _json_response({"message": "boom"}, 500)])
    async with client:
        with pytest.raises(SfeHttpError, match="500"):
            await monitor.fetch()


# --- key() -----------------------------------------------------------------


def test_key_is_the_job_id_alone_not_a_content_hash() -> None:
    # An edited job (a changed subject, a shifted end time) must not re-alert --
    # the ask is "a new position was posted", not "this posting changed" -- and a
    # job that is claimed and then released must re-alert, because that is a
    # genuine new opportunity. Keying on the id alone gives both for free.
    monitor, _ = make_monitor([])
    edited_a = Job(job_id=1, location_name="X", job_start="s1", job_end="e1", classf_name="MATH")
    edited_b = Job(job_id=1, location_name="X", job_start="s2", job_end="e2", classf_name="ART")
    other_job = Job(job_id=2, location_name="X", job_start="s1", job_end="e1")
    assert monitor.key(edited_a) == monitor.key(edited_b) == "1"
    assert monitor.key(other_job) == "2"


# --- render() --------------------------------------------------------------


async def test_render_resolves_dates_per_job() -> None:
    # One message per job, each with its own date line pulled from that job's own
    # detail fetch -- not one shared line for the whole batch.
    jobs = [
        Job(
            job_id=1025535,
            location_name="RALSTON VALLEY HS",
            job_start="2026-10-16T13:45Z",
            job_end="2026-10-19T21:30Z",
            classf_name="SEC MATH",
        ),
        Job(
            job_id=1025548,
            location_name="WHEAT RIDGE HIGH SCHOOL",
            job_start="2026-09-04T13:45Z",
            job_end="2026-09-04T21:30Z",
            classf_name="SEC MATH",
        ),
    ]
    monitor, client = make_monitor(
        [*_login_responses(), _json_response(MULTIDAY), _json_response(SINGLE)]
    )
    async with client:
        messages = await monitor.render(jobs)

    assert len(messages) == 2
    first = messages[0].payload.embeds[0].description
    second = messages[1].payload.embeds[0].description
    assert "Fri Oct 16 · Mon Oct 19, 7:45 AM – 3:30 PM" in first  # noqa: RUF001 -- EN DASH
    assert "Fri Sep 4, 7:45 AM – 3:30 PM" in second  # noqa: RUF001 -- EN DASH
    assert "approximate" not in first
    assert "approximate" not in second


async def test_a_failed_detail_fetch_degrades_only_that_one_job() -> None:
    # Under the library, a render() raise withholds every fresh key in the
    # batch (monitor.runner.run_tick) -- so one unreachable detail endpoint must
    # degrade one job's dates rather than silence the whole tick's alerts. This
    # matters more in Python than it did in TypeScript for exactly that reason.
    jobs = [
        Job(
            job_id=1025535,
            location_name="RALSTON VALLEY HS",
            job_start="2026-10-16T13:45Z",
            job_end="2026-10-19T21:30Z",
            classf_name="SEC MATH",
        ),
        Job(
            job_id=999,
            location_name="LAKEWOOD HIGH SCHOOL",
            job_start="2026-11-02T15:40Z",
            job_end="2026-11-02T22:40Z",
            days_of_week="M",
        ),
    ]
    monitor, client = make_monitor(
        [*_login_responses(), _json_response(MULTIDAY), _json_response({"message": "boom"}, 500)]
    )
    async with client:
        messages = await monitor.render(jobs)

    assert len(messages) == 2
    assert "approximate" not in messages[0].payload.embeds[0].description
    assert "approximate, check SFE" in messages[1].payload.embeds[0].description


# --- heartbeat_extras() -----------------------------------------------------


def test_heartbeat_extras_with_no_gaps() -> None:
    monitor, _ = make_monitor([])
    extras = monitor.heartbeat_extras()
    assert extras.fields == ()
    assert extras.footer_text is None  # keeps the library's default footer
