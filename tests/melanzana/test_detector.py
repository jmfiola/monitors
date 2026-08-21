from melanzana.detector import MonthKey, filter_bookable, months_to_fetch
from melanzana.types import Slot

NOW = 1796000000  # 2026-11-30 00:53 UTC
DAY = 86400


def slot(key: str, start_unix: int, qty_left: int = 4, is_bookable: bool = True) -> Slot:
    return Slot(key=key, start_unix=start_unix, qty_left=qty_left, is_bookable=is_bookable)


def test_enumerates_every_month_the_window_spans() -> None:
    assert months_to_fetch(NOW, 60) == [
        MonthKey(2026, 11),
        MonthKey(2026, 12),
        MonthKey(2027, 1),
    ]


def test_includes_the_prior_denver_month_at_a_utc_month_boundary() -> None:
    # 2026-12-01 03:00 UTC is still 2026-11-30 in Denver (UTC-7); a near-term slot
    # would be in the NOVEMBER api query. The start pad must cover it.
    boundary = 1796094000
    assert months_to_fetch(boundary, 60) == [
        MonthKey(2026, 11),
        MonthKey(2026, 12),
        MonthKey(2027, 1),
        MonthKey(2027, 2),
    ]


def test_rolls_the_year_over_at_december() -> None:
    assert months_to_fetch(NOW, 400)[-1] == MonthKey(2028, 1)


def test_keeps_only_in_window_bookable_slots_with_spots_left() -> None:
    slots = [
        slot("2026-12-01 10:30", 1796146200, 4, True),  # in window, bookable -> KEEP
        slot("2026-12-01 11:00", 1796148000, 0, True),  # qty 0 -> DROP
        slot("2026-12-01 11:30", 1796149800, 4, False),  # not bookable -> DROP
        slot("past", NOW - DAY, 4, True),  # before now -> DROP
        slot("far", NOW + 61 * DAY, 4, True),  # beyond 60d -> DROP
    ]
    assert [s.key for s in filter_bookable(slots, NOW, 60)] == ["2026-12-01 10:30"]


def test_the_window_boundaries_are_inclusive() -> None:
    slots = [slot("now", NOW), slot("edge", NOW + 60 * DAY)]
    assert [s.key for s in filter_bookable(slots, NOW, 60)] == ["now", "edge"]
