from dataclasses import replace

from monitor.config import RunnerConfig, load_runner_config
from monitor.health import HealthState, init_health
from monitor.runner import run_liveness
from monitor.types import Field, HeartbeatExtras, OpsLabels, Payload

LABELS = OpsLabels(name="X monitor", tracked_noun="thing(s)", death_footer="footer.")


def cfg_with(**env: str) -> RunnerConfig:
    return load_runner_config(
        {"DISCORD_WEBHOOK_URL": "https://discord.test/webhook", **env},
        labels=LABELS,
        log_prefix="x-monitor",
        default_poll_interval_sec=10,
    )


def noop_log(_message: str) -> None:
    return None


class Collector:
    """A Poster that records every ops message it is handed."""

    def __init__(self) -> None:
        self.posts: list[Payload] = []

    async def __call__(self, url: str, payload: Payload) -> None:
        self.posts.append(payload)


class ExplodingExtras:
    """An app whose heartbeat_extras() raises — must degrade, never propagate."""

    def __call__(self) -> HeartbeatExtras:
        raise RuntimeError("gap set exploded")


async def test_success_resets_the_failure_count_and_records_the_poll() -> None:
    posts = Collector()
    health = await run_liveness(
        cfg_with(),
        replace(init_health(1000), consecutive_failures=3),
        outcome="success",
        items_tracked=5,
        now=1100,
        heartbeat_extras=HeartbeatExtras,
        poster=posts,
        log=noop_log,
    )
    assert health.consecutive_failures == 0
    assert health.last_success_unix == 1100
    assert health.items_tracked == 5
    assert posts.posts == []


async def test_failure_increments_the_counter_without_alerting_before_the_threshold() -> None:
    posts = Collector()
    health = await run_liveness(
        cfg_with(),
        init_health(1000),
        outcome="failure",
        items_tracked=0,
        now=1100,
        heartbeat_extras=HeartbeatExtras,
        poster=posts,
        log=noop_log,
    )
    assert health.consecutive_failures == 1
    assert health.death_alerted is False
    assert posts.posts == []


async def test_one_death_alert_is_latched_then_one_recovery_is_posted() -> None:
    posts = Collector()
    cfg = cfg_with()
    health: HealthState = init_health(1000)
    for now in (1300, 1700, 2000):  # 300 < 600; 700 >= 600 -> death; then latched
        health = await run_liveness(
            cfg,
            health,
            outcome="failure",
            items_tracked=0,
            now=now,
            heartbeat_extras=HeartbeatExtras,
            poster=posts,
            log=noop_log,
        )
    assert len(posts.posts) == 1
    assert posts.posts[0].content is None  # an ops message never pings
    assert health.death_alerted is True

    health = await run_liveness(
        cfg,
        health,
        outcome="success",
        items_tracked=2,
        now=2100,
        heartbeat_extras=HeartbeatExtras,
        poster=posts,
        log=noop_log,
    )
    assert len(posts.posts) == 2
    assert health.death_alerted is False


async def test_the_heartbeat_waits_for_its_interval() -> None:
    posts = Collector()
    cfg = cfg_with()
    health = await run_liveness(
        cfg,
        init_health(1000),
        outcome="success",
        items_tracked=1,
        now=1000 + 86399,
        heartbeat_extras=HeartbeatExtras,
        poster=posts,
        log=noop_log,
    )
    assert posts.posts == []
    health = await run_liveness(
        cfg,
        health,
        outcome="success",
        items_tracked=1,
        now=1000 + 86400,
        heartbeat_extras=HeartbeatExtras,
        poster=posts,
        log=noop_log,
    )
    assert len(posts.posts) == 1
    assert health.last_heartbeat_unix == 1000 + 86400


async def test_the_heartbeat_is_suppressed_while_latched_dead_and_resumes_after_recovery() -> None:
    # The death alert already signals liveness, and a "still watching" message
    # mid-outage would contradict it. The recovery alert restarts the cadence, so a
    # heartbeat that came due during the outage does not double up right after.
    posts = Collector()
    cfg = cfg_with()
    health = await run_liveness(
        cfg,
        init_health(1000),
        outcome="failure",
        items_tracked=0,
        now=2000,
        heartbeat_extras=HeartbeatExtras,
        poster=posts,
        log=noop_log,
    )
    assert len(posts.posts) == 1  # death only
    health = await run_liveness(
        cfg,
        health,
        outcome="failure",
        items_tracked=0,
        now=2000 + 86400,
        heartbeat_extras=HeartbeatExtras,
        poster=posts,
        log=noop_log,
    )
    assert len(posts.posts) == 1  # still just the death alert
    health = await run_liveness(
        cfg,
        health,
        outcome="success",
        items_tracked=1,
        now=2000 + 86401,
        heartbeat_extras=HeartbeatExtras,
        poster=posts,
        log=noop_log,
    )
    assert len(posts.posts) == 2  # death + recovery, no heartbeat between
    assert health.death_alerted is False
    assert health.last_heartbeat_unix == 2000 + 86401


async def test_a_heartbeat_carries_the_apps_extras_and_honours_the_configured_hour() -> None:
    posts = Collector()
    cfg = cfg_with(HEARTBEAT_AT="07:00")
    dec1_0700, dec1_2300, dec2_0700 = 1796133600, 1796191200, 1796220000
    gap = Field(name="gaps", value="• X", inline=False)
    health = init_health(dec1_0700)

    health = await run_liveness(
        cfg,
        health,
        outcome="success",
        items_tracked=3,
        now=dec1_2300,
        heartbeat_extras=HeartbeatExtras,
        poster=posts,
        log=noop_log,
    )
    assert posts.posts == []  # same local day

    health = await run_liveness(
        cfg,
        health,
        outcome="success",
        items_tracked=3,
        now=dec2_0700,
        heartbeat_extras=lambda: HeartbeatExtras(fields=(gap,), footer_text="do this"),
        poster=posts,
        log=noop_log,
    )
    assert len(posts.posts) == 1
    assert posts.posts[0].embeds[0].fields == (gap,)
    assert posts.posts[0].embeds[0].footer_text == "do this"


async def test_extras_that_raise_degrade_to_a_plain_heartbeat() -> None:
    # An app-side error must not take down the loop whose job is to report that the
    # app is alive — that is a permanently failed unit with nothing to raise the alarm.
    posts = Collector()
    logged: list[str] = []
    health = await run_liveness(
        cfg_with(),
        init_health(1000),
        outcome="success",
        items_tracked=1,
        now=1000 + 86400,
        heartbeat_extras=ExplodingExtras(),
        poster=posts,
        log=logged.append,
    )
    assert len(posts.posts) == 1  # the heartbeat still went out
    assert posts.posts[0].embeds[0].fields is None
    assert posts.posts[0].embeds[0].footer_text == "Routine heartbeat — no action needed."
    assert health.last_heartbeat_unix == 1000 + 86400
    assert any("heartbeat_extras() raised" in line for line in logged)


async def test_extras_are_not_called_when_no_heartbeat_is_due() -> None:
    # Assembling them can cost real work, so it happens only when one is going out.
    calls = 0

    def counting_extras() -> HeartbeatExtras:
        nonlocal calls
        calls += 1
        return HeartbeatExtras()

    await run_liveness(
        cfg_with(),
        init_health(1000),
        outcome="success",
        items_tracked=1,
        now=1100,
        heartbeat_extras=counting_extras,
        poster=Collector(),
        log=noop_log,
    )
    assert calls == 0


async def test_a_failing_ops_post_cannot_escape_or_strand_the_death_latch() -> None:
    # If this propagated, death_alerted would stay False and the death alert would
    # re-post on every tick forever.
    async def always_fails(url: str, payload: Payload) -> None:
        raise RuntimeError("HTTP 500")

    logged: list[str] = []
    health = await run_liveness(
        cfg_with(STALL_ALERT_SEC="1"),
        init_health(1000),
        outcome="failure",
        items_tracked=0,
        now=2000,
        heartbeat_extras=HeartbeatExtras,
        poster=always_fails,
        log=logged.append,
    )
    assert health.death_alerted is True
    assert any("status post failed" in line for line in logged)
