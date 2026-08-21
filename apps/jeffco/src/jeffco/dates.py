"""Date rendering for the alert. All labels are built from explicit tables rather
than strftime: %a and %b are locale-dependent, and %-I (hour without a leading
zero) is a glibc/BSD extension Python does not guarantee. This is the same
construction melanzana's alert.py uses, which has a byte-identical differential
result behind it.

The TypeScript source builds these labels with Intl.DateTimeFormat(...).formatToParts,
which throws RangeError on an invalid Date. Python's datetime.fromisoformat raises
ValueError instead -- format_approximate is the one place that must catch it and
degrade rather than propagate, because it is the fallback path used when the
detail fetch has already failed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from jeffco.types import Job, JobDay

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # isoweekday() - 1
MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# U+2013 (EN DASH), matching the TypeScript's time/day ranges -- not a hyphen.
_RANGE_SEP = "–"  # noqa: RUF001 -- intentional, contract with the TypeScript output
# U+00B7 (MIDDLE DOT), joining day labels: "Fri Oct 16 <dot> Mon Oct 19".
_DAY_SEP = "·"


def format_day_label(unix_sec: int, timezone: str) -> str:
    """Render e.g. "Fri Oct 16", in the given zone."""
    local = datetime.fromtimestamp(unix_sec, ZoneInfo(timezone))
    return f"{WEEKDAYS[local.isoweekday() - 1]} {MONTHS[local.month - 1]} {local.day}"


def format_time_label(unix_sec: int, timezone: str) -> str:
    """Render e.g. "7:45 AM", in the given zone. Hour unpadded, minute padded."""
    local = datetime.fromtimestamp(unix_sec, ZoneInfo(timezone))
    hour = local.hour % 12 or 12  # 0 -> 12 and 12 -> 12; `% 12` alone gives 0
    meridiem = "AM" if local.hour < 12 else "PM"
    return f"{hour}:{local.minute:02d} {meridiem}"


def parse_job_days(detail: object) -> list[JobDay]:
    """Read the worked days out of a GET /api/job/{id} response. This is the
    authoritative record: daysOfWeek on the list response cannot express a job
    that runs Friday and the following Monday, and its one-letter tokens are
    ambiguous between Tuesday and Thursday.

    Tolerant by design: a shape change upstream yields an empty list, and the
    caller falls back to the approximate rendering rather than raising.
    """
    if not isinstance(detail, dict):
        return []
    raw = detail.get("jobDetails")
    if not isinstance(raw, list):
        return []

    days: list[JobDay] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sub_start = entry.get("subStart")
        sub_end = entry.get("subEnd")
        if not isinstance(sub_start, str) or not isinstance(sub_end, str):
            continue
        try:
            start_unix = int(datetime.fromisoformat(sub_start).timestamp())
            end_unix = int(datetime.fromisoformat(sub_end).timestamp())
        except ValueError:
            continue
        days.append(JobDay(start_unix, end_unix))

    return sorted(days, key=lambda d: d.start_unix)


def _time_range(day: JobDay, timezone: str) -> str:
    start = format_time_label(day.start_unix, timezone)
    end = format_time_label(day.end_unix, timezone)
    return f"{start} {_RANGE_SEP} {end}"


def format_job_days(days: Sequence[JobDay], timezone: str) -> str:
    """Render worked days for the alert. When every day shares one schedule (the
    norm) the times are stated once: "Fri Oct 16, Mon Oct 19, 7:45 AM - 3:30 PM"
    (joined with the middle dot, ranges with the en dash). When they differ, each
    day carries its own times rather than implying a schedule that is wrong for
    some of them.
    """
    if not days:
        return "Dates unavailable — check SFE"

    sorted_days = sorted(days, key=lambda d: d.start_unix)
    times = [_time_range(d, timezone) for d in sorted_days]
    uniform = all(t == times[0] for t in times)
    if uniform:
        labels = f" {_DAY_SEP} ".join(format_day_label(d.start_unix, timezone) for d in sorted_days)
        return f"{labels}, {times[0]}"

    return f" {_DAY_SEP} ".join(
        f"{format_day_label(d.start_unix, timezone)} {times[i]}" for i, d in enumerate(sorted_days)
    )


def format_approximate(job: Job, timezone: str) -> str:
    """Degraded rendering from the list row alone, used when the detail fetch
    fails. Explicitly labelled approximate because the jobStart/jobEnd span is
    not a day list: a two-endpoint range may mean two days or four. Vague beats
    silent.
    """
    days_of_week = job.days_of_week.strip() if job.days_of_week else ""
    tokens = f" (days: {days_of_week})" if days_of_week else ""

    try:
        start_unix = int(datetime.fromisoformat(job.job_start).timestamp())
        end_unix = int(datetime.fromisoformat(job.job_end).timestamp())
    except (ValueError, OverflowError, OSError):
        # ValueError is the one that actually fires. OverflowError and OSError are
        # here because `.timestamp()` can raise them for datetimes near year 1 or
        # 9999 on some platforms, and this function is the *degraded* path -- it
        # runs when the detail fetch has already failed. Anything escaping it
        # propagates out of `render()`, which withholds every fresh key in the
        # batch and then retries the identical failure forever, so one malformed
        # row would silence all alerting indefinitely. Catch broadly here.
        return f"Dates unavailable{tokens} — check SFE"

    start_day = format_day_label(start_unix, timezone)
    end_day = format_day_label(end_unix, timezone)
    day_range = start_day if start_day == end_day else f"{start_day} {_RANGE_SEP} {end_day}"
    start_time = format_time_label(start_unix, timezone)
    end_time = format_time_label(end_unix, timezone)
    time_range = f"{start_time} {_RANGE_SEP} {end_time}"
    return f"{day_range}, {time_range}{tokens} — approximate, check SFE"
