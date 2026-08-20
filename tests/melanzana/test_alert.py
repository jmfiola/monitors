from melanzana.alert import format_alert
from melanzana.types import Slot
from monitor.types import GREEN

BOOKING_URL = "https://melanzana.com/pages/how-to-shop"


def slot(key: str, qty_left: int = 4) -> Slot:
    return Slot(key=key, start_unix=1796146200, qty_left=qty_left, is_bookable=True)


def test_groups_slots_into_one_inline_day_card_per_day_with_a_summary() -> None:
    payload = format_alert(
        [slot("2026-12-01 10:30", 4), slot("2026-12-01 11:00", 2), slot("2026-12-02 09:00", 5)],
        booking_url=BOOKING_URL,
        mention_everyone=True,
    )
    embed = payload.embeds[0]
    assert embed.title == "🟢 New Melanzana appointment(s) available!"
    assert embed.url == BOOKING_URL
    assert embed.description == "3 open slot(s) across 2 day(s):"
    assert embed.color == GREEN
    assert embed.footer_text == "Tap the title to book — slots go fast."
    assert embed.fields is not None
    assert len(embed.fields) == 2
    # 2026-12-01 is a Tuesday, 2026-12-02 a Wednesday (UTC).
    assert embed.fields[0].name == "📅 Tue, Dec 1"
    assert embed.fields[0].value == "10:30 — 4 left\n11:00 — 2 left"
    assert embed.fields[0].inline is True
    assert embed.fields[1].name == "📅 Wed, Dec 2"
    assert embed.fields[1].value == "09:00 — 5 left"


def test_orders_days_and_times_chronologically_regardless_of_input_order() -> None:
    payload = format_alert(
        [slot("2026-12-02 09:00"), slot("2026-12-01 11:00"), slot("2026-12-01 10:30")],
        booking_url=BOOKING_URL,
        mention_everyone=True,
    )
    fields = payload.embeds[0].fields
    assert fields is not None
    assert [f.name for f in fields] == ["📅 Tue, Dec 1", "📅 Wed, Dec 2"]
    assert fields[0].value == "10:30 — 4 left\n11:00 — 4 left"


def test_adds_the_everyone_pair_when_mentioning() -> None:
    payload = format_alert(
        [slot("2026-12-01 10:30")], booking_url=BOOKING_URL, mention_everyone=True
    )
    assert payload.content == "@everyone"
    assert payload.allowed_mentions_parse == ("everyone",)


def test_omits_the_mention_when_not_mentioning() -> None:
    payload = format_alert(
        [slot("2026-12-01 10:30")], booking_url=BOOKING_URL, mention_everyone=False
    )
    assert payload.content is None
    assert payload.allowed_mentions_parse is None
    assert "content" not in payload.to_dict()
    assert "allowed_mentions" not in payload.to_dict()


def test_caps_the_day_cards_and_notes_the_remaining_days() -> None:
    # 30 distinct days (Sep 1-30) -> 24 day-cards + 1 overflow note field.
    # Discord allows 25 embed fields; one is reserved for the note.
    many = [slot(f"2026-09-{day:02d} 10:00") for day in range(1, 31)]
    fields = format_alert(many, booking_url=BOOKING_URL, mention_everyone=True).embeds[0].fields
    assert fields is not None
    assert len(fields) == 25
    assert fields[24].name == "…"
    assert fields[24].value == "and 6 more day(s) — tap the title to see all."
    assert fields[24].inline is False


def test_falls_back_gracefully_for_a_key_without_a_parseable_date() -> None:
    fields = (
        format_alert([slot("weird-key")], booking_url=BOOKING_URL, mention_everyone=True)
        .embeds[0]
        .fields
    )
    assert fields is not None
    assert fields[0].name == "📅 weird-key"
    assert fields[0].value == "weird-key — 4 left"


def test_accepts_the_iso_t_separator_in_a_key() -> None:
    fields = (
        format_alert([slot("2026-12-01T10:30")], booking_url=BOOKING_URL, mention_everyone=False)
        .embeds[0]
        .fields
    )
    assert fields is not None
    assert fields[0].name == "📅 Tue, Dec 1"
    assert fields[0].value == "10:30 — 4 left"
