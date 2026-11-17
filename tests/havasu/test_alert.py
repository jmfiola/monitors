from datetime import date, timedelta

from havasu.alert import FOOTER, MAX_DATE_CARDS, format_alert
from havasu.types import OpenArrival

URL = "https://bookingsus.newbook.cloud/online/havasupai"


def test_one_card_per_arrival_in_date_order() -> None:
    payload = format_alert(
        [OpenArrival(date(2027, 5, 10)), OpenArrival(date(2027, 5, 8))], booking_url=URL
    )
    embed = payload.embeds[0]
    assert embed.url == URL
    assert embed.description == "2 arrival date(s) open for a 3-night stay:"
    assert embed.footer_text == FOOTER
    assert embed.fields is not None
    assert [(f.name, f.value) for f in embed.fields] == [
        ("📅 Sat, May 8, 2027", "out Tue, May 11"),
        ("📅 Mon, May 10, 2027", "out Thu, May 13"),
    ]
    assert payload.content is None  # never @everyone


def test_sale_day_overflows_into_a_note() -> None:
    start = date(2027, 5, 8)
    arrivals = [OpenArrival(start + timedelta(days=i)) for i in range(40)]
    fields = format_alert(arrivals, booking_url=URL).embeds[0].fields
    assert fields is not None
    assert len(fields) == MAX_DATE_CARDS + 1
    assert fields[-1].value == "and 16 more date(s) — tap the title to see all."
