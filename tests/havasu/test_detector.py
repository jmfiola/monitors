from datetime import date

from havasu.detector import open_arrivals, query_window
from havasu.newbook import parse_calendar
from havasu.types import DayState, OpenArrival, WatchRange, YearMonth

from tests.havasu.helpers import CAL_2026, fixture_json

TODAY = date(2026, 11, 16)
PRODUCTION = (
    WatchRange(date(2027, 5, 8), date(2027, 5, 31)),
    WatchRange(date(2027, 10, 1), date(2027, 10, 24)),
)


def nov_2026() -> dict[date, DayState]:
    return parse_calendar(fixture_json(CAL_2026), YearMonth(2026, 3), YearMonth(2026, 11))


def test_the_window_reaches_the_last_departure_day() -> None:
    assert query_window(PRODUCTION, TODAY) == (YearMonth(2027, 5), YearMonth(2027, 10))
    late = (WatchRange(date(2027, 5, 1), date(2027, 5, 30)),)
    assert query_window(late, TODAY) == (YearMonth(2027, 5), YearMonth(2027, 6))


def test_all_dates_spans_this_month_through_next_december() -> None:
    assert query_window(None, TODAY) == (YearMonth(2026, 11), YearMonth(2027, 12))


def test_matches_what_the_booking_chart_said_on_2026_11_16() -> None:
    # The chart offered "Book now" for Nov 16 and Nov 28 and no sites for Nov 29
    # and Nov 30 — their third night or later falls on a closed December.
    arrivals = [a.arrival for a in open_arrivals(nov_2026(), None, TODAY)]
    assert arrivals == [date(2026, 11, d) for d in range(16, 29)]


def test_past_days_never_open() -> None:
    arrivals = open_arrivals(nov_2026(), None, date(2026, 11, 20))
    assert arrivals[0] == OpenArrival(date(2026, 11, 20))


def test_a_range_bounds_only_the_arrival_date() -> None:
    ranges = (WatchRange(date(2026, 11, 20), date(2026, 11, 24)),)
    arrivals = [a.arrival for a in open_arrivals(nov_2026(), ranges, TODAY)]
    # Nov 24 arrives in range and departs Nov 27, outside it — still watched.
    assert arrivals == [date(2026, 11, d) for d in range(20, 25)]


def test_closed_arrival_and_closed_departure_block_the_stay() -> None:
    days = nov_2026()
    days[date(2026, 11, 20)] = DayState(available=True, closed_arrival=True)
    days[date(2026, 11, 25)] = DayState(available=True, closed_departure=True)
    arrivals = {a.arrival for a in open_arrivals(days, None, TODAY)}
    assert date(2026, 11, 20) not in arrivals  # no arrivals that day
    assert date(2026, 11, 22) not in arrivals  # departs Nov 25, which is closed
    assert date(2026, 11, 21) in arrivals
    assert date(2026, 11, 25) in arrivals  # arriving on a closed-departure day is fine


def test_one_booked_night_closes_the_three_arrivals_that_need_it() -> None:
    days = nov_2026()
    days[date(2026, 11, 22)] = DayState(available=False)
    arrivals = {a.arrival for a in open_arrivals(days, None, TODAY)}
    assert {date(2026, 11, d) for d in (20, 21, 22)}.isdisjoint(arrivals)
    assert {date(2026, 11, 19), date(2026, 11, 23)} <= arrivals
