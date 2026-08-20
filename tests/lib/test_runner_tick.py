from dataclasses import dataclass

import pytest
from monitor.config import RunnerConfig, load_runner_config
from monitor.discord import DiscordPostError
from monitor.runner import run_tick
from monitor.types import Embed, HeartbeatExtras, Message, OpsLabels, Payload

LABELS = OpsLabels(name="X monitor", tracked_noun="thing(s)", death_footer="footer.")


def make_cfg() -> RunnerConfig:
    return load_runner_config(
        {"DISCORD_WEBHOOK_URL": "https://discord.test/webhook"},
        labels=LABELS,
        log_prefix="x-monitor",
        default_poll_interval_sec=10,
    )


@dataclass(frozen=True)
class Thing:
    id: str


def payload_for(*ids: str) -> Payload:
    return Payload(embeds=(Embed(title=" ".join(ids), description="d", color=1),))


class FakeMonitor:
    """One message per item by default, so `covers` isolation is observable.

    `one_message=True` is melanzana's shape: a single message covering every key.
    `cover_only` drops keys from the messages entirely, to exercise the guard
    against a render that silently fails to announce something.
    """

    def __init__(
        self,
        items: list[Thing],
        *,
        one_message: bool = False,
        cover_only: set[str] | None = None,
        render_error: Exception | None = None,
    ) -> None:
        self.items = items
        self.one_message = one_message
        self.cover_only = cover_only
        self.render_error = render_error
        self.rendered: list[list[str]] = []

    async def fetch(self) -> list[Thing]:
        return self.items

    def key(self, item: Thing) -> str:
        return item.id

    async def render(self, new: list[Thing]) -> list[Message]:
        self.rendered.append([t.id for t in new])
        if self.render_error is not None:
            raise self.render_error
        chosen = [t for t in new if self.cover_only is None or t.id in self.cover_only]
        if self.one_message:
            ids = tuple(t.id for t in chosen)
            return [Message(payload=payload_for(*ids), covers=ids)]
        return [Message(payload=payload_for(t.id), covers=(t.id,)) for t in chosen]

    def heartbeat_extras(self) -> HeartbeatExtras:
        return HeartbeatExtras()


class Recorder:
    """A poster that records, and fails on chosen payload titles with a chosen status."""

    def __init__(self, fail_titles: set[str] | None = None, status: int | None = 500) -> None:
        self.posted: list[str] = []
        self.urls: list[str] = []
        self.fail_titles = fail_titles or set()
        self.status = status

    async def __call__(self, url: str, payload: Payload) -> None:
        title = payload.embeds[0].title
        self.urls.append(url)
        if title in self.fail_titles:
            if self.status is None:
                raise RuntimeError(f"connection reset posting {title}")
            raise DiscordPostError(f"Discord webhook failed: HTTP {self.status}", self.status)
        self.posted.append(title)


class StatusRecorder:
    def __init__(self) -> None:
        self.posts: list[Payload] = []

    async def __call__(self, payload: Payload) -> None:
        self.posts.append(payload)


async def noop_sleep(_sec: float) -> None:
    return None


async def drive(
    monitor: FakeMonitor,
    previous: set[str],
    *,
    is_first_run: bool = False,
    poster: Recorder | None = None,
    status: StatusRecorder | None = None,
    sleep: object = noop_sleep,
    logged: list[str] | None = None,
) -> set[str]:
    """Call run_tick with the boilerplate filled in."""
    return await run_tick(
        monitor,
        make_cfg(),
        previous,
        is_first_run=is_first_run,
        poster=poster or Recorder(),
        post_status=status or StatusRecorder(),
        sleep=sleep,  # type: ignore[arg-type]
        log=(logged.append if logged is not None else (lambda _m: None)),
    )


async def test_first_run_records_the_baseline_and_posts_nothing() -> None:
    monitor = FakeMonitor([Thing("a"), Thing("b")])
    poster = Recorder()
    keys = await drive(monitor, set(), is_first_run=True, poster=poster)
    assert poster.posted == []
    assert monitor.rendered == []  # render is never even called
    assert keys == {"a", "b"}


async def test_posts_only_the_items_absent_from_the_previous_key_set() -> None:
    monitor = FakeMonitor([Thing("a"), Thing("b")])
    poster = Recorder()
    keys = await drive(monitor, {"a"}, poster=poster)
    assert monitor.rendered == [["b"]]
    assert poster.posted == ["b"]
    assert keys == {"a", "b"}


async def test_posts_nothing_when_every_item_persists() -> None:
    poster = Recorder()
    keys = await drive(FakeMonitor([Thing("a")]), {"a"}, poster=poster)
    assert poster.posted == []
    assert keys == {"a"}


async def test_one_item_returned_twice_is_announced_once() -> None:
    # Divergence #3. melanzana's padded month queries overlap, so the same slot can
    # arrive twice in one tick; the TypeScript would render "2 open slot(s)" for one
    # slot and print its line twice.
    monitor = FakeMonitor([Thing("a"), Thing("a")], one_message=True)
    poster = Recorder()
    keys = await drive(monitor, set(), poster=poster)
    assert monitor.rendered == [["a"]]
    assert poster.posted == ["a"]
    assert keys == {"a"}


async def test_spaces_the_messages_of_a_batch_but_not_the_first() -> None:
    # The overwhelmingly common tick has exactly one item, and it should reach the
    # phone as fast as it always did.
    slept: list[float] = []

    async def record_sleep(sec: float) -> None:
        slept.append(sec)

    await drive(FakeMonitor([Thing("a"), Thing("b"), Thing("c")]), set(), sleep=record_sleep)
    assert slept == [make_cfg().post_spacing_sec, make_cfg().post_spacing_sec]


async def test_a_failed_post_withholds_only_that_messages_keys() -> None:
    # Divergence #1: the tick does not raise, the unannounced key is withheld so
    # the next tick re-alerts it, and every other key is banked.
    poster = Recorder(fail_titles={"b"}, status=503)
    logged: list[str] = []
    keys = await drive(
        FakeMonitor([Thing("a"), Thing("b"), Thing("c")]), set(), poster=poster, logged=logged
    )
    assert poster.posted == ["a", "c"]
    assert keys == {"a", "c"}
    assert any("will retry 1 item(s)" in line for line in logged)


async def test_a_failed_multi_item_message_withholds_every_key_it_covered() -> None:
    # Melanzana's shape: one message covering every fresh key.
    poster = Recorder(fail_titles={"a b"}, status=503)
    keys = await drive(
        FakeMonitor([Thing("a"), Thing("b")], one_message=True), set(), poster=poster
    )
    assert poster.posted == []
    assert keys == set()


async def test_a_transport_error_with_no_status_is_treated_as_retryable() -> None:
    poster = Recorder(fail_titles={"a"}, status=None)
    keys = await drive(FakeMonitor([Thing("a")]), set(), poster=poster)
    assert keys == set()


async def test_a_failed_post_does_not_withhold_a_key_that_still_exists_upstream() -> None:
    # `covers` is subtracted from the *current* key set, so an item that was
    # already in the baseline is unaffected by another message's failure.
    poster = Recorder(fail_titles={"b"}, status=503)
    keys = await drive(FakeMonitor([Thing("a"), Thing("b")]), {"a"}, poster=poster)
    assert poster.posted == []  # only "b" was fresh, and it failed
    assert keys == {"a"}


async def test_a_permanent_rejection_banks_the_keys_and_posts_an_ops_alert() -> None:
    # Retrying a 400 forever is a way of never noticing. Bank the keys so the loop
    # stops, and say out loud that these items will not be announced.
    poster = Recorder(fail_titles={"a"}, status=400)
    status = StatusRecorder()
    logged: list[str] = []
    keys = await drive(
        FakeMonitor([Thing("a")]), set(), poster=poster, status=status, logged=logged
    )
    assert keys == {"a"}  # banked, NOT withheld
    assert len(status.posts) == 1
    assert "HTTP 400" in status.posts[0].embeds[0].description
    assert any("permanently rejected" in line for line in logged)


async def test_a_429_abandons_the_rest_of_the_batch() -> None:
    # Continuing at 0.35s spacing would be 2.86 req/s into a webhook that just said
    # stop. The unattempted messages are withheld, so the next tick re-sends them.
    poster = Recorder(fail_titles={"a"}, status=429)
    logged: list[str] = []
    keys = await drive(
        FakeMonitor([Thing("a"), Thing("b"), Thing("c")]), set(), poster=poster, logged=logged
    )
    assert poster.posted == []
    assert keys == set()
    assert any("rate limited" in line for line in logged)


async def test_a_render_failure_withholds_everything_without_failing_the_poll() -> None:
    # The fetch succeeded. Escalating backoff here would eventually post "No
    # successful poll since … may be blocked or down", which is simply untrue.
    monitor = FakeMonitor([Thing("a")], render_error=RuntimeError("detail fetch exploded"))
    logged: list[str] = []
    keys = await drive(monitor, set(), logged=logged)
    assert keys == set()
    assert any("render failed" in line for line in logged)


async def test_a_key_no_message_covers_is_withheld_rather_than_banked() -> None:
    # An app whose render quietly drops an item must not have that item recorded as
    # seen — that is a silent swallow, forever.
    monitor = FakeMonitor([Thing("a"), Thing("b")], cover_only={"a"})
    poster = Recorder()
    keys = await drive(monitor, set(), poster=poster)
    assert poster.posted == ["a"]
    assert keys == {"a"}


async def test_a_fetch_failure_propagates() -> None:
    # No data is not the same as "nothing available"; treating it as an empty list
    # would wipe the baseline and re-alert everything.
    class Broken(FakeMonitor):
        async def fetch(self) -> list[Thing]:
            raise RuntimeError("upstream 503")

    with pytest.raises(RuntimeError, match="503"):
        await drive(Broken([]), set())


async def test_a_mixed_batch_banks_the_delivered_and_the_permanent_but_withholds_the_retryable() -> (  # noqa: E501
    None
):
    # The three outcomes have to coexist in one tick, because they share one `settled`
    # set. Without this, banking every fresh key regardless of outcome passes every
    # other test in this file — and that is the swallow-an-item-forever direction.
    monitor = FakeMonitor([Thing("a"), Thing("b"), Thing("c")])
    status = StatusRecorder()

    class Mixed:
        def __init__(self) -> None:
            self.posted: list[str] = []

        async def __call__(self, url: str, payload: Payload) -> None:
            title = payload.embeds[0].title
            if title == "b":
                raise DiscordPostError("HTTP 503", 503)  # retryable -> withhold
            if title == "c":
                raise DiscordPostError("HTTP 400", 400)  # permanent -> bank
            self.posted.append(title)

    poster = Mixed()
    keys = await run_tick(
        monitor,
        make_cfg(),
        set(),
        is_first_run=False,
        poster=poster,
        post_status=status,
        sleep=noop_sleep,
        log=lambda _m: None,
    )
    assert poster.posted == ["a"]
    assert keys == {"a", "c"}  # a delivered, c abandoned, b comes back next tick
    assert len(status.posts) == 1  # exactly one delivery-failure alert, for c


async def test_an_item_that_vanished_upstream_leaves_the_baseline() -> None:
    # The baseline is rebuilt from what fetch() returned, never unioned with the old
    # one. A union would look harmless and would mean an item that disappears and
    # comes back is never announced again.
    poster = Recorder()
    keys = await drive(FakeMonitor([Thing("a")]), {"a", "gone"}, poster=poster)
    assert keys == {"a"}
    assert "gone" not in keys
    assert poster.posted == []


async def test_an_item_that_returns_after_vanishing_is_announced_again() -> None:
    poster = Recorder()
    keys = await drive(FakeMonitor([Thing("a")]), set(), poster=poster)
    assert keys == {"a"} and poster.posted == ["a"]
    # It disappears: dropped from the baseline.
    assert await drive(FakeMonitor([]), keys, poster=Recorder()) == set()
    # It comes back: fresh again, and announced again.
    poster2 = Recorder()
    assert await drive(FakeMonitor([Thing("a")]), set(), poster=poster2) == {"a"}
    assert poster2.posted == ["a"]


async def test_alerts_go_to_the_alert_webhook_not_the_status_channel() -> None:
    cfg = load_runner_config(
        {
            "DISCORD_WEBHOOK_URL": "https://discord.test/webhook",
            "STATUS_WEBHOOK_URL": "https://discord.test/ops",
        },
        labels=LABELS,
        log_prefix="x-monitor",
        default_poll_interval_sec=10,
    )
    poster = Recorder()
    await run_tick(
        FakeMonitor([Thing("a")]),
        cfg,
        set(),
        is_first_run=False,
        poster=poster,
        post_status=StatusRecorder(),
        sleep=noop_sleep,
        log=lambda _m: None,
    )
    assert poster.urls == ["https://discord.test/webhook"]
