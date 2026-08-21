import json
from pathlib import Path

from jeffco.dates import (
    format_approximate,
    format_day_label,
    format_job_days,
    format_time_label,
    parse_job_days,
)
from jeffco.types import Job, JobDay

DENVER = "America/Denver"

FIXTURES = Path(__file__).parent / "fixtures"
MULTIDAY = json.loads((FIXTURES / "job-detail-multiday.json").read_text(encoding="utf-8"))
SINGLE = json.loads((FIXTURES / "job-detail-single.json").read_text(encoding="utf-8"))
CONTIGUOUS = json.loads((FIXTURES / "job-detail-contiguous.json").read_text(encoding="utf-8"))

# Pinned from:
#   python3 -c "
#   from datetime import datetime
#   from zoneinfo import ZoneInfo
#   D = ZoneInfo('America/Denver')
#   for label, dt in [...]:
#       print(f'{label:20} {int(dt.timestamp())}  {dt.isoformat()}')
#   "
# MDT is UTC-6, MST is UTC-7; the March pair straddles the 2026 spring-forward
# transition (2026-03-08 02:00 MST -> 03:00 MDT).
MDT_OCT16_0745 = 1792158300  # 2026-10-16T07:45:00-06:00, Fri
MDT_OCT16_1530 = 1792186200  # 2026-10-16T15:30:00-06:00, Fri
MDT_OCT19_0745 = 1792417500  # 2026-10-19T07:45:00-06:00, Mon
MDT_OCT19_1530 = 1792445400  # 2026-10-19T15:30:00-06:00, Mon
MDT_OCT19_1200 = 1792432800  # 2026-10-19T12:00:00-06:00, Mon
MIDNIGHT_MAR8 = 1772953200  # 2026-03-08T00:00:00-07:00, Sun (still MST)
MST_MAR6_0745 = 1772808300  # 2026-03-06T07:45:00-07:00, Fri (MST)
MST_MAR6_1530 = 1772836200  # 2026-03-06T15:30:00-07:00, Fri (MST)
MDT_MAR9_0745 = 1773063900  # 2026-03-09T07:45:00-06:00, Mon (MDT, after the jump)
MDT_MAR9_1530 = 1773091800  # 2026-03-09T15:30:00-06:00, Mon (MDT, after the jump)
OCT17_0400_UTC = 1792209600  # 2026-10-17T04:00:00Z -- still Oct 16 in Denver


def test_day_and_time_labels() -> None:
    assert format_day_label(MDT_OCT16_0745, DENVER) == "Fri Oct 16"
    assert format_time_label(MDT_OCT16_0745, DENVER) == "7:45 AM"
    assert format_time_label(MDT_OCT16_1530, DENVER) == "3:30 PM"


def test_uses_the_configured_zone_not_the_host_zone() -> None:
    # 2026-10-17T04:00Z is still Oct 16 in Denver. A host-local formatter on a
    # UTC server would say Oct 17 and send a substitute to school on the wrong day.
    assert format_day_label(OCT17_0400_UTC, DENVER) == "Fri Oct 16"
    assert format_day_label(OCT17_0400_UTC, "UTC") == "Sat Oct 17"


def test_the_hour_has_no_leading_zero_and_the_minute_does() -> None:
    # This is why strftime is not used: %-I is not portable and %I would give "07".
    assert format_time_label(MDT_OCT16_0745, DENVER).startswith("7:45")


def test_noon_and_midnight_render_as_12() -> None:
    # A 12-hour clock built by hand gets 0 and 12 wrong if you use `% 12` alone.
    assert format_time_label(MIDNIGHT_MAR8, DENVER) == "12:00 AM"


def test_a_uniform_schedule_states_the_times_once() -> None:
    days = [JobDay(MDT_OCT16_0745, MDT_OCT16_1530), JobDay(MDT_OCT19_0745, MDT_OCT19_1530)]
    assert format_job_days(days, DENVER) == "Fri Oct 16 · Mon Oct 19, 7:45 AM – 3:30 PM"  # noqa: RUF001


def test_differing_schedules_carry_their_own_times() -> None:
    # Stating one schedule that is wrong for some days is worse than being verbose.
    days = [JobDay(MDT_OCT16_0745, MDT_OCT16_1530), JobDay(MDT_OCT19_0745, MDT_OCT19_1200)]
    out = format_job_days(days, DENVER)
    assert out == "Fri Oct 16 7:45 AM – 3:30 PM · Mon Oct 19 7:45 AM – 12:00 PM"  # noqa: RUF001


def test_a_job_spanning_the_spring_forward_keeps_each_days_own_wall_clock() -> None:
    # THE case this module is most likely to get wrong. Both days start at 7:45
    # local, but the UTC offset differs across the transition -- so a naive
    # implementation that computes one offset and reuses it renders one day an
    # hour off. Both must read 7:45 AM.
    days = [JobDay(MST_MAR6_0745, MST_MAR6_1530), JobDay(MDT_MAR9_0745, MDT_MAR9_1530)]
    out = format_job_days(days, DENVER)
    assert out == "Fri Mar 6 · Mon Mar 9, 7:45 AM – 3:30 PM"  # noqa: RUF001
    assert out.count("7:45 AM") == 1  # uniform, so stated once


def test_renders_a_single_day_without_a_separator() -> None:
    assert format_job_days(parse_job_days(SINGLE), DENVER) == "Fri Sep 4, 7:45 AM – 3:30 PM"  # noqa: RUF001


def test_converts_utc_to_mountain_standard_time_after_the_dst_boundary() -> None:
    # November is MST (UTC-7), so 15:40Z is 8:40 AM -- an hour later than the same
    # UTC clock time would render in October's MDT.
    out = format_job_days(parse_job_days(CONTIGUOUS), DENVER)
    assert out == "Wed Nov 4 · Thu Nov 5 · Fri Nov 6, 8:40 AM – 3:40 PM"  # noqa: RUF001


def test_no_days_says_so_rather_than_rendering_nothing() -> None:
    assert format_job_days([], DENVER) == "Dates unavailable — check SFE"


def test_parse_job_days_sorts_and_tolerates_garbage() -> None:
    detail = {
        "jobDetails": [
            {"subStart": "2026-10-19T13:45:00Z", "subEnd": "2026-10-19T21:30:00Z"},
            {"subStart": "2026-10-16T13:45:00Z", "subEnd": "2026-10-16T21:30:00Z"},
            {"subStart": None, "subEnd": "2026-10-20T21:30:00Z"},
            "not an object",
        ]
    }
    days = parse_job_days(detail)
    assert len(days) == 2
    assert days[0].start_unix < days[1].start_unix


def test_reads_one_entry_per_worked_day_from_the_real_multiday_fixture() -> None:
    days = parse_job_days(MULTIDAY)
    assert len(days) == 2
    assert days[0].start_unix == MDT_OCT16_0745
    assert days[1].start_unix == MDT_OCT19_0745


def test_parse_job_days_returns_empty_on_a_shape_change() -> None:
    # Tolerant by design: the caller falls back to the approximate rendering.
    assert parse_job_days({}) == []
    assert parse_job_days({"jobDetails": "nope"}) == []
    assert parse_job_days(None) == []


def test_approximate_is_labelled_approximate() -> None:
    j = Job(
        job_id=1,
        location_name="X",
        job_start="2026-10-16T13:45Z",
        job_end="2026-10-19T21:30Z",
        days_of_week="F M",
    )
    out = format_approximate(j, DENVER)
    assert "approximate, check SFE" in out
    assert "(days: F M)" in out


def test_collapses_a_single_day_range_to_one_date() -> None:
    j = Job(
        job_id=1,
        location_name="X",
        job_start="2026-10-16T13:45Z",
        job_end="2026-10-16T21:30Z",
        days_of_week="F",
    )
    out = format_approximate(j, DENVER)
    assert out == "Fri Oct 16, 7:45 AM – 3:30 PM (days: F) — approximate, check SFE"  # noqa: RUF001


def test_omits_the_day_tokens_when_sfe_sends_none() -> None:
    j = Job(
        job_id=1,
        location_name="X",
        job_start="2026-10-16T13:45Z",
        job_end="2026-10-16T21:30Z",
        days_of_week="",
    )
    out = format_approximate(j, DENVER)
    assert out == "Fri Oct 16, 7:45 AM – 3:30 PM — approximate, check SFE"  # noqa: RUF001


def test_approximate_degrades_rather_than_raising_on_an_unparseable_date() -> None:
    # This is the path that has to hold when everything else has already failed.
    j = Job(job_id=1, location_name="X", job_start="not-a-date", job_end="also-not")
    assert format_approximate(j, DENVER) == "Dates unavailable — check SFE"
