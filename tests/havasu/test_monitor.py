from datetime import date

import httpx
from havasu.config import load_config
from havasu.monitor import HavasuMonitor
from havasu.types import OpenArrival

from tests.havasu.helpers import FakeNewBook

WEBHOOK = "https://discord.test/webhook"
#: 2026-11-17 02:00 UTC, which is still Nov 16 (19:00) in America/Phoenix — so a
#: UTC "today" would wrongly drop the Nov 16 arrival.
NOW = 1794880800


def make_monitor(fake: FakeNewBook, **env: str) -> tuple[HavasuMonitor, httpx.AsyncClient]:
    cfg = load_config({"DISCORD_WEBHOOK_URL": WEBHOOK, **env})
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return HavasuMonitor(cfg, client=client, now_unix=lambda: NOW), client


async def test_fetch_returns_watched_open_arrivals() -> None:
    fake = FakeNewBook()
    # The fixture answers for 2026-03..2026-11, so ask for exactly that span.
    monitor, client = make_monitor(fake, WATCH_RANGES="2026-03-01..2026-11-27")
    async with client:
        items = await monitor.fetch()

    assert [i.arrival for i in items] == [date(2026, 11, d) for d in range(16, 28)]


def test_the_key_is_the_arrival_date() -> None:
    monitor, _ = make_monitor(FakeNewBook())
    assert monitor.key(OpenArrival(date(2027, 5, 8))) == "campground:2027-05-08"


async def test_render_covers_every_fresh_key_in_one_message() -> None:
    monitor, client = make_monitor(FakeNewBook())
    fresh = [OpenArrival(date(2027, 5, 8)), OpenArrival(date(2027, 10, 24))]
    async with client:
        messages = await monitor.render(fresh)
    assert len(messages) == 1
    assert messages[0].covers == ("campground:2027-05-08", "campground:2027-10-24")


def test_the_heartbeat_says_what_is_watched() -> None:
    monitor, _ = make_monitor(FakeNewBook(), WATCH_RANGES="all")
    (field,) = monitor.heartbeat_extras().fields
    assert (field.name, field.value) == ("Watching arrivals", "all dates")
