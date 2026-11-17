from datetime import date
from typing import Any

import httpx
import pytest
from havasu.newbook import NewBookError, NewBookSource, parse_calendar
from havasu.types import DayState, YearMonth

from tests.havasu.helpers import (
    CAL_2026,
    CAL_2027,
    CAL_ALL,
    FakeNewBook,
    fixture_json,
    with_rows,
)

MAR, NOV = YearMonth(2026, 3), YearMonth(2026, 11)


def test_parses_every_day_of_the_captured_2026_calendar() -> None:
    days = parse_calendar(fixture_json(CAL_2026), MAR, NOV)

    assert len(days) == 275
    open_days = sorted(d for d, s in days.items() if s.available)
    assert open_days == [date(2026, 11, d) for d in range(16, 31)]
    assert days[date(2026, 11, 16)] == DayState(available=True, closed_departure=True)
    assert days[date(2026, 11, 17)] == DayState(available=True)
    assert days[date(2026, 11, 15)] == DayState(available=False)


def test_a_season_not_yet_on_sale_reads_as_all_booked() -> None:
    days = parse_calendar(fixture_json(CAL_2027), YearMonth(2026, 12), YearMonth(2027, 12))
    assert len(days) == 396
    assert not any(s.available for s in days.values())


def test_parses_the_real_closed_arrival_cell() -> None:
    days = parse_calendar(fixture_json(CAL_ALL), NOV, YearMonth(2027, 12))
    assert len(days) == 426
    assert days[date(2026, 12, 1)] == DayState(available=False, closed_arrival=True)


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        (
            'class="day available" data-date="2026-11-20"',
            'class="day available sparkly" data-date="2026-11-20"',
            "unknown class",
        ),
        (
            'class="day available" data-date="2026-11-20"',
            'class="day booked available" data-date="2026-11-20"',
            "state tokens",
        ),
        (
            'class="day available" data-date="2026-11-20"',
            'class="day available" data-date="2026-11-xx"',
            "unparseable date",
        ),
        (
            'class="day available" data-date="2026-11-20"',
            'class="day available" data-date="2026-11-21"',
            "repeats",
        ),
        ('<td class="empty">', '<td class="filler">', "without a date"),
    ],
)
def test_an_unrecognized_cell_is_a_fault(old: str, new: str, match: str) -> None:
    payload = fixture_json(CAL_2026)
    rows = payload["calendar_data"]["table_rows"]
    assert old in rows
    with pytest.raises(NewBookError, match=match):
        parse_calendar(with_rows(payload, rows.replace(old, new, 1)), MAR, NOV)


def test_a_missing_day_is_a_fault_not_a_closed_day() -> None:
    payload = fixture_json(CAL_2026)
    rows = payload["calendar_data"]["table_rows"].replace(
        '<td class="day available" data-date="2026-11-20">20</td>', ""
    )
    with pytest.raises(NewBookError, match="1 missing"):
        parse_calendar(with_rows(payload, rows), MAR, NOV)


def test_an_answer_for_a_different_period_is_a_fault() -> None:
    with pytest.raises(NewBookError, match="asked for"):
        parse_calendar(fixture_json(CAL_2026), YearMonth(2026, 4), NOV)


@pytest.mark.parametrize("payload", [[], {"period_from": "2026-03", "period_to": "2026-11"}])
def test_a_malformed_payload_is_a_fault(payload: Any) -> None:
    with pytest.raises(NewBookError):
        parse_calendar(payload, MAR, NOV)


async def test_bootstraps_once_and_posts_the_calendar_query() -> None:
    fake = FakeNewBook()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
        source = NewBookSource(client)
        await source.fetch_calendar(MAR, NOV)
        await source.fetch_calendar(MAR, NOV)

    assert fake.page_loads == 1
    form = fake.forms[0]
    assert form["query"] == ["newbook_calendar_update_table_dates"]
    assert form["category_id"] == ["1"]
    assert form["adults"] == ["1"]
    assert form["nights"] == ["3"]
    assert (form["period_from"], form["period_to"]) == (["2026-03"], ["2026-11"])
    assert (form["available_from"], form["available_to"]) == (["Mar 1 2026"], ["Mar 4 2026"])
    assert not any("cart" in v for values in form.values() for v in values)


async def test_any_failure_drops_the_session_and_its_cookie() -> None:
    fake = FakeNewBook(calendar=httpx.Response(200, text="<html>session expired</html>"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
        source = NewBookSource(client)
        with pytest.raises(NewBookError, match="not JSON"):
            await source.fetch_calendar(MAR, NOV)
        fake.calendar = httpx.Response(503)
        with pytest.raises(NewBookError, match="503"):
            await source.fetch_calendar(MAR, NOV)

    assert fake.page_loads == 2
    assert fake.cookies_sent == [None, None]  # the stale cookie is not carried over


async def test_a_page_without_the_api_path_is_a_fault() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, text="<html></html>"))
    ) as client:
        with pytest.raises(NewBookError, match="newbook_api_path"):
            await NewBookSource(client).fetch_calendar(MAR, NOV)
