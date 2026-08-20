import httpx
from melanzana.config import load_config
from melanzana.monitor import MelanzanaMonitor
from melanzana.types import Slot

WEBHOOK = "https://discord.test/webhook"
NOW = 1796000000
FIXTURE_SLOT = {
    "slot": "2026-12-01 10:30",
    "slot_start_unix": 1796146200,
    "is_bookable": True,
    "qty_left": 4,
}


def make_monitor(
    handler: httpx.MockTransport, **env: str
) -> tuple[MelanzanaMonitor, httpx.AsyncClient]:
    cfg = load_config({"DISCORD_WEBHOOK_URL": WEBHOOK, **env})
    client = httpx.AsyncClient(transport=handler)
    return MelanzanaMonitor(cfg, client=client, now_unix=lambda: NOW), client


def test_the_key_is_the_slot_string() -> None:
    monitor, _ = make_monitor(httpx.MockTransport(lambda _r: httpx.Response(200, json={})))
    slot = Slot(key="2026-12-01 10:30", start_unix=1796146200, qty_left=4, is_bookable=True)
    assert monitor.key(slot) == "2026-12-01 10:30"


async def test_fetch_queries_every_month_in_the_window_and_returns_only_bookables() -> None:
    queried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queried.append(f"{request.url.params['year']}-{request.url.params['month']}")
        return httpx.Response(
            200,
            json={
                "long": [
                    FIXTURE_SLOT,
                    # Dropped by the window cut, so it must not reach the runner.
                    {
                        "slot": "2026-12-01 12:00",
                        "slot_start_unix": 1796146200,
                        "is_bookable": True,
                        "qty_left": 0,
                    },
                ]
            },
        )

    monitor, client = make_monitor(httpx.MockTransport(handler))
    async with client:
        items = await monitor.fetch()

    assert sorted(queried) == ["2026-11", "2026-12", "2027-1"]
    # The mock answers every month query with the same slot, so fetch() legitimately
    # returns it three times — the padded months overlap and nothing here de-dupes.
    # That is exactly the case run_tick now collapses (divergence #3); asserting the
    # list rather than a set keeps the boundary honest about who owns it.
    assert [s.key for s in items] == ["2026-12-01 10:30"] * 3


async def test_render_returns_one_message_covering_every_fresh_key() -> None:
    monitor, client = make_monitor(
        httpx.MockTransport(lambda _r: httpx.Response(200, json={})), MENTION_EVERYONE="true"
    )
    fresh = [
        Slot(key="2026-12-01 10:30", start_unix=1796146200, qty_left=4, is_bookable=True),
        Slot(key="2026-12-02 09:00", start_unix=1796232600, qty_left=5, is_bookable=True),
    ]
    async with client:
        messages = await monitor.render(fresh)

    assert len(messages) == 1
    assert messages[0].covers == ("2026-12-01 10:30", "2026-12-02 09:00")
    assert messages[0].payload.content == "@everyone"
    assert messages[0].payload.embeds[0].description == "2 open slot(s) across 2 day(s):"


def test_heartbeat_extras_are_empty_because_melanzana_has_nothing_to_add() -> None:
    monitor, _ = make_monitor(httpx.MockTransport(lambda _r: httpx.Response(200, json={})))
    extras = monitor.heartbeat_extras()
    assert extras.fields == ()
    assert extras.footer_text is None  # keeps the library's default footer


async def test_a_month_query_failure_propagates_so_the_runner_backs_off() -> None:
    import pytest
    from melanzana.cowlendar import CowlendarError

    monitor, client = make_monitor(httpx.MockTransport(lambda _r: httpx.Response(503)))
    async with client:
        with pytest.raises(CowlendarError, match="503"):
            await monitor.fetch()
