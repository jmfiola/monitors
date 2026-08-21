import locale
from datetime import datetime

import pytest
from jeffco.dates import format_day_label, format_time_label
from melanzana.alert import format_alert
from melanzana.types import Slot

MDT_OCT16_0745 = 1792158300


@pytest.mark.parametrize("locale_name", ["de_DE.UTF-8", "fr_FR.UTF-8", "ja_JP.UTF-8"])
def test_explicit_date_tables_ignore_lc_time_when_strftime_does_not(locale_name: str) -> None:
    previous = locale.setlocale(locale.LC_TIME)
    try:
        try:
            locale.setlocale(locale.LC_TIME, locale_name)
        except locale.Error:
            pytest.skip(f"{locale_name} is not installed")

        assert datetime(2026, 10, 16, 13, 45).strftime("%a %b %d %p") != "Fri Oct 16 PM"
        assert format_day_label(MDT_OCT16_0745, "America/Denver") == "Fri Oct 16"
        assert format_time_label(MDT_OCT16_0745, "America/Denver") == "7:45 AM"

        slot = Slot("2026-10-16 13:45", MDT_OCT16_0745, 1, True)
        payload = format_alert([slot], booking_url="https://example.test", mention_everyone=False)
        fields = payload.embeds[0].fields
        assert fields is not None
        assert fields[0].name == "📅 Fri, Oct 16"
    finally:
        locale.setlocale(locale.LC_TIME, previous)
