"""Dump every Discord payload jeffco can produce, as canonical JSON.

Half of the differential harness. The TypeScript half lives at
`jeffco-sub-monitor/tools/dump-payloads.ts` and must print byte-identical output.
This is the layer the port is trusted on: the cycle deliberately does not do a
shadow run, so an empty diff here is the evidence that the rewrite renders what
the original rendered.

Both sides pin the clock and build the synthetic cases from the same literals, so
any difference in the output is a difference in the *implementations*. Verified in
melanzana's harness: Node's JSON.stringify(canon(x), null, 2) and this
json.dumps(..., sort_keys=True, indent=2, ensure_ascii=False) agree byte for byte.

No expected output is written down on either side. Every literal here is an *input*
-- Job fields, epochs, clock integers, fixture names -- or an import from production
code, and every rendered byte comes from each language's own production functions.
That is the property that makes an empty diff mean something: there is nothing for
the two sides to agree on except behaviour.

**Keep floats out of payloads.** A 30-value probe found exactly one place these two
serializers disagree: a whole-valued float prints `3.0` in Python and `3` in Node.
Nothing float-valued reaches a payload today (`uptime_hours` uses `//`), so this is
latent rather than live -- but the first float that does will produce a diff that
looks like a port regression and is really a serializer difference. The direction is
at least fail-safe: Node can never emit `3.0`, so it fails loudly rather than
silently matching.

**The ops messages are deliberately out of scope**, which is why the cases stop at
6. The heartbeat, its filter-gap report, and the liveness alerts have all diverged
from the TypeScript on purpose: Python is the source of truth for them now, its
timestamps are absolute-with-relative rather than bare relative, and its gap report
spends a character budget instead of a fixed ten names. Comparing them could only
produce a permanent diff, and a permanent diff destroys the meaning of an empty
one -- the same reason the busy payload was never a case here. They are covered by
tests/lib/test_discord.py and tests/jeffco/test_alert.py instead.

What that costs: LABELS was previously *imported* here rather than restated, so
this harness caught drift in the ops wording too. It no longer does; the label
assertions in tests/jeffco/test_alert.py are what hold it.

Case 5 calls format_approximate directly rather than simulating a failed detail
fetch: this compares *rendering*, and a mocked failure would go through
httpx.MockTransport on this side and something else entirely on the other, making
the two sides disagree about something that is not the output.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jeffco.alert import format_job_alerts
from jeffco.dates import format_approximate, format_job_days, parse_job_days
from jeffco.sfe import parse_jobs
from jeffco.types import Job, JobDay

#: The district and the API both run in Denver time; jeffco.config pins the same
#: value rather than reading it from the environment.
TIMEZONE = "America/Denver"

FIXTURES = Path(__file__).resolve().parents[1] / "tests/jeffco/fixtures"

#: Which detail fixture belongs to which row of available-jobs.json. A job with no
#: entry here gets the degraded rendering, which is what a real batch looks like
#: when one detail fetch fails.
DETAIL_BY_JOB_ID = {
    1025548: "job-detail-single",
    1025535: "job-detail-multiday",
    1025543: "job-detail-contiguous",
}

#: The fixture's two-day job happens to run the same hours on both days, so it takes
#: the uniform branch. These synthetic days take the other one -- each day carrying
#: its own times, joined by the middle dot -- and cross the 12:00 AM / 12:00 PM
#: boundary while they are there, where `hour % 12 or 12` and Intl's hour numbering
#: could disagree about noon and midnight.
DIFFERING_DAYS = (
    JobDay(1792130400, 1792173600),  # Fri 2026-10-16, 00:00-12:00 MDT
    JobDay(1792434600, 1792475940),  # Mon 2026-10-19, 12:30-23:59 MDT
)

#: A job spanning the 2026 spring-forward Sunday, synthetic on both sides. The two
#: start epochs are 82800s apart -- 23 hours, not 24 -- and both must still render
#: "7:45 AM", which is the whole point of the case. zoneinfo and Intl are both
#: IANA-backed and should agree; "should" is what a fixture is for.
DST_DAYS = (
    JobDay(1772894700, 1772922600),  # Sat 2026-03-07, 07:45-15:30 MST (-07:00)
    JobDay(1772977500, 1773005400),  # Sun 2026-03-08, 07:45-15:30 MDT (-06:00)
)
DST_JOB = Job(
    job_id=9000001,
    location_name="GOLDEN HIGH SCHOOL",
    job_start="2026-03-07T14:45Z",
    job_end="2026-03-08T21:30Z",
    classf_name="SEC SCIENCE",
    employee_first_name="Dana",
    employee_last_name="Whitfield",
    days_of_week="Sa Su",
    duration_type="FULL",
)

#: The degraded path's other half: parse_jobs guarantees these fields are strings,
#: not that they are dates. Python raises ValueError where Intl throws RangeError,
#: and both sides must land on the same "Dates unavailable" line. This row also
#: carries no subject, no teacher and a non-FULL duration -- three description
#: guards no fixture row exercises.
#:
#: `not-a-date` is load-bearing and must NOT be "strengthened" to a well-formed
#: impossible date like 2026-02-30. Measured:
#:
#:     Date.parse("not-a-date")  -> NaN            datetime.fromisoformat -> ValueError
#:     Date.parse("2026-02-30")  -> 1772409600000  datetime.fromisoformat -> ValueError
#:                                  (silently 2026-03-02)
#:
#: Garbage is rejected by both, so both degrade and the bytes match. An out-of-range
#: day is rolled over by JavaScript and rejected by Python, so the two sides would
#: render a plausible wrong date against "Dates unavailable" -- a real, deliberate
#: divergence, which is exactly what a parity harness must never contain. It would
#: read as a regression and destroy the meaning of the empty diff.
BROKEN_DATE_JOB = Job(
    job_id=9000002,
    location_name="ARVADA HIGH SCHOOL",
    job_start="not-a-date",
    job_end="not-a-date",
    days_of_week="  F  ",  # padded, to pin the strip()/trim()
    duration_type="HALF DAY AM",
)


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _date_line(job: Job) -> str:
    """The date line a real tick would build for this job: from the detail fixture
    when there is one, degraded when there is not."""
    name = DETAIL_BY_JOB_ID.get(job.job_id)
    if name is None:
        return format_approximate(job, TIMEZONE)
    return format_job_days(parse_job_days(_fixture(name)), TIMEZONE)


def _payloads(alerts: Sequence[tuple[Job, str]]) -> list[dict[str, Any]]:
    """Just the payloads. `Message.covers` is state bookkeeping the library added
    and has no TypeScript counterpart; the harness compares what a user sees."""
    return [message.payload.to_dict() for message in format_job_alerts(alerts)]


def build() -> dict[str, Any]:
    jobs = parse_jobs(_fixture("available-jobs"))
    by_id = {job.job_id: job for job in jobs}
    single = by_id[1025548]
    contiguous = by_id[1025543]
    multiday = by_id[1025535]

    return {
        "1-single-day": _payloads([(single, _date_line(single))]),
        "2-contiguous": _payloads([(contiguous, _date_line(contiguous))]),
        "3-multiday": _payloads(
            [
                (multiday, _date_line(multiday)),
                (multiday, format_job_days(DIFFERING_DAYS, TIMEZONE)),
            ]
        ),
        # Every row, in fixture order: one message per job, and their order.
        "4-batch": _payloads([(job, _date_line(job)) for job in jobs]),
        "5-approximate": _payloads(
            [
                (multiday, format_approximate(multiday, TIMEZONE)),
                (BROKEN_DATE_JOB, format_approximate(BROKEN_DATE_JOB, TIMEZONE)),
            ]
        ),
        "6-dst-span": _payloads([(DST_JOB, format_job_days(DST_DAYS, TIMEZONE))]),
    }


def main() -> None:
    print(json.dumps(build(), sort_keys=True, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
