import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from monitor.config import RunnerConfig, load_runner_config
from monitor.runner import run_forever
from monitor.types import Embed, HeartbeatExtras, Message, OpsLabels, Payload, SourceBusy

LABELS = OpsLabels(name="X monitor", tracked_noun="thing(s)", death_footer="footer.")


def cfg_for(state_path: Path, **env: str) -> RunnerConfig:
    return load_runner_config(
        {
            "DISCORD_WEBHOOK_URL": "https://discord.test/webhook",
            "STATE_PATH": str(state_path),
            "POLL_JITTER_PCT": "0",
            **env,
        },
        labels=LABELS,
        log_prefix="x-monitor",
        default_poll_interval_sec=10,
    )


@dataclass(frozen=True)
class Thing:
    id: str


class ScriptedMonitor:
    """Returns a scripted batch per tick; a batch may be an exception to raise."""

    def __init__(self, script: list[list[Thing] | Exception]) -> None:
        self.script = script
        self.tick = 0

    async def fetch(self) -> list[Thing]:
        batch = self.script[min(self.tick, len(self.script) - 1)]
        self.tick += 1
        if isinstance(batch, Exception):
            raise batch
        return batch

    def key(self, item: Thing) -> str:
        return item.id

    async def render(self, new: list[Thing]) -> list[Message]:
        return [
            Message(
                payload=Payload(embeds=(Embed(title=t.id, description="d", color=1),)),
                covers=(t.id,),
            )
            for t in new
        ]

    def heartbeat_extras(self) -> HeartbeatExtras:
        return HeartbeatExtras()


class Harness:
    def __init__(self) -> None:
        self.posted: list[str] = []
        self.slept: list[float] = []
        self.logged: list[str] = []
        self.clock = 1000

    async def poster(self, url: str, payload: Payload) -> None:
        self.posted.append(payload.embeds[0].title)

    async def sleep(self, sec: float) -> None:
        self.slept.append(sec)
        self.clock += int(sec)

    def now(self) -> int:
        return self.clock


async def test_the_loop_baselines_then_alerts_and_persists(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    harness = Harness()
    monitor = ScriptedMonitor([[Thing("a")], [Thing("a"), Thing("b")]])

    await run_forever(
        monitor,
        cfg_for(state),
        poster=harness.poster,
        sleep=harness.sleep,
        now_unix=harness.now,
        rand=lambda: 0.5,
        log=harness.logged.append,
        max_ticks=2,
    )

    assert harness.posted == ["b"]  # tick 1 baselines silently, tick 2 alerts
    assert json.loads(state.read_text(encoding="utf-8")) == ["a", "b"]
    assert harness.slept == [10, 10]  # jitter 0 => exactly the interval


async def test_a_fault_escalates_backoff_and_leaves_the_baseline_alone(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text('["a"]', encoding="utf-8")
    harness = Harness()
    monitor = ScriptedMonitor([RuntimeError("upstream 503")])

    await run_forever(
        monitor,
        cfg_for(state),
        poster=harness.poster,
        sleep=harness.sleep,
        now_unix=harness.now,
        rand=lambda: 0.5,
        log=harness.logged.append,
        max_ticks=3,
    )

    assert harness.slept == [10, 20, 40]  # doubling from the poll interval
    assert json.loads(state.read_text(encoding="utf-8")) == ["a"]
    assert any("backing off" in line for line in harness.logged)


async def test_source_busy_holds_the_cadence_and_does_not_climb(tmp_path: Path) -> None:
    # SFE answers 400 while the account holder's own session is active. Doubling
    # the gap would blind the monitor for minutes at exactly the moment he is on
    # the site claiming the job the last alert announced.
    harness = Harness()
    monitor = ScriptedMonitor([SourceBusy("HTTP 400 — account busy")])

    await run_forever(
        monitor,
        cfg_for(tmp_path / "state.json"),
        poster=harness.poster,
        sleep=harness.sleep,
        now_unix=harness.now,
        rand=lambda: 0.5,
        log=harness.logged.append,
        max_ticks=3,
    )

    assert harness.slept == [10, 10, 10]
    assert any("busy" in line for line in harness.logged)


async def test_source_busy_leaves_an_existing_backoff_ladder_where_it_was(tmp_path: Path) -> None:
    # A real fault arriving later still climbs from where it left off rather than
    # restarting at one interval.
    harness = Harness()
    monitor = ScriptedMonitor([RuntimeError("fault"), SourceBusy("busy"), RuntimeError("fault")])

    await run_forever(
        monitor,
        cfg_for(tmp_path / "state.json"),
        poster=harness.poster,
        sleep=harness.sleep,
        now_unix=harness.now,
        rand=lambda: 0.5,
        log=harness.logged.append,
        max_ticks=3,
    )

    assert harness.slept == [10, 10, 20]


async def test_a_failed_post_keeps_the_cadence_and_counts_as_a_healthy_tick(
    tmp_path: Path,
) -> None:
    # Divergence #1, at the loop level: no backoff, no health failure, and the
    # withheld key is re-alerted on the next tick.
    state = tmp_path / "state.json"
    state.write_text("[]", encoding="utf-8")
    harness = Harness()
    monitor = ScriptedMonitor([[Thing("a")]])
    attempts: list[str] = []

    async def flaky_poster(url: str, payload: Payload) -> None:
        title = payload.embeds[0].title
        attempts.append(title)
        if len(attempts) == 1:
            raise RuntimeError("HTTP 500")

    await run_forever(
        monitor,
        cfg_for(state),
        poster=flaky_poster,
        sleep=harness.sleep,
        now_unix=harness.now,
        rand=lambda: 0.5,
        log=harness.logged.append,
        max_ticks=2,
    )

    assert attempts == ["a", "a"]  # withheld, then re-alerted
    assert harness.slept == [10, 10]  # normal cadence throughout
    assert json.loads(state.read_text(encoding="utf-8")) == ["a"]


async def test_a_state_write_failure_is_logged_and_the_loop_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Divergence #2. A write failure reported as a *poll* failure would throttle
    # polling 5x, latch a false death alert, and suppress the heartbeat — all while
    # alerts were arriving normally. The in-memory baseline is what the process
    # runs on; durability is best-effort.
    harness = Harness()
    monitor = ScriptedMonitor([[Thing("a")], [Thing("a"), Thing("b")]])

    def unwritable(path: str, keys: set[str]) -> None:
        raise OSError("read-only file system")

    # The string form, not `monkeypatch.setattr(monitor.runner, ...)`: the local
    # variable `monitor` above shadows the module of the same name.
    monkeypatch.setattr("monitor.runner.save_state", unwritable)

    await run_forever(
        monitor,
        cfg_for(tmp_path / "state.json"),
        poster=harness.poster,
        sleep=harness.sleep,
        now_unix=harness.now,
        rand=lambda: 0.5,
        log=harness.logged.append,
        max_ticks=2,
    )

    assert harness.posted == ["b"]  # the in-memory baseline advanced, so tick 2 diffed
    assert harness.slept == [10, 10]  # no backoff
    assert any("state write" in line for line in harness.logged)


async def test_a_corrupt_baseline_file_says_so(tmp_path: Path) -> None:
    # Missing and corrupt both read as first run, and they mean opposite things.
    # Corrupt means everything open right now goes unannounced — the failure that
    # looks exactly like everything being fine.
    state = tmp_path / "state.json"
    state.write_text("not json{{{", encoding="utf-8")
    harness = Harness()

    await run_forever(
        ScriptedMonitor([[Thing("a")]]),
        cfg_for(state),
        poster=harness.poster,
        sleep=harness.sleep,
        now_unix=harness.now,
        rand=lambda: 0.5,
        log=harness.logged.append,
        max_ticks=1,
    )

    assert any("did not parse" in line for line in harness.logged)


async def test_a_status_post_failure_never_disturbs_polling(tmp_path: Path) -> None:
    # The ops message has to be one that actually fires, which a two-tick run of a
    # healthy monitor never produces: tick 1 is first-run and posts nothing, tick 2
    # diffs empty. SourceBusy plus STALL_ALERT_SEC=1 latches a death alert on tick 2
    # while holding the poll cadence, so the failing post is provably reached and the
    # `[10, 10]` assertion still means something.
    harness = Harness()

    async def always_fails(url: str, payload: Payload) -> None:
        raise RuntimeError("HTTP 500")

    await run_forever(
        ScriptedMonitor([SourceBusy("account busy")]),
        cfg_for(tmp_path / "state.json", STALL_ALERT_SEC="1"),
        poster=always_fails,
        sleep=harness.sleep,
        now_unix=harness.now,
        rand=lambda: 0.5,
        log=harness.logged.append,
        max_ticks=2,
    )

    assert harness.slept == [10, 10]  # cadence held: no backoff, no crash
    assert any("status post failed" in line for line in harness.logged)
