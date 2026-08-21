"""The poll loop. The contract an app implements to be driven by it is in types.py."""

from __future__ import annotations

import asyncio
import functools
import random
import signal
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Literal

from monitor.config import RunnerConfig, make_log
from monitor.discord import (
    PAYLOAD_REJECTED_STATUS,
    DiscordPostError,
    Poster,
    StatusPoster,
    format_delivery_failure,
    format_heartbeat,
    format_status_alert,
)
from monitor.health import HealthState, init_health, should_alert_stall, should_heartbeat
from monitor.state import load_state, save_state
from monitor.timing import next_backoff, system_now, with_jitter
from monitor.types import HeartbeatExtras, Monitor, Payload, SourceBusy


async def run_tick[Item](
    monitor: Monitor[Item],
    cfg: RunnerConfig,
    previous_keys: set[str],
    *,
    is_first_run: bool,
    poster: Poster,
    post_status: StatusPoster,
    sleep: Callable[[float], Awaitable[None]],
    log: Callable[[str], None],
) -> set[str]:
    """One poll cycle: fetch, key, diff, render, post. Returns the new baseline.

    On the very first run there is no baseline to diff against, so everything would
    look new. Suppressing here rather than offering a config knob: `echo '[]' >
    state.json` is the supported way to ask for the current backlog, and it makes
    the run not-first by construction.

    A key is left out of the returned baseline — "withheld" — whenever it was not
    successfully announced and a retry might still work. Everything else is banked.

    `post_status` must swallow its own errors. It is awaited from inside an exception
    handler, so a raise there would turn a tick that polled successfully into a
    reported poll failure — backoff, a false death alert, and a suppressed heartbeat,
    all while alerting was merely degraded.
    """
    items = await monitor.fetch()

    # De-duplicated by key, which the TypeScript does not do. melanzana's month
    # enumeration pads and therefore overlaps, so one slot can arrive twice in a
    # single tick — and a duplicate would inflate the alert's own "N open slot(s)"
    # count and repeat its day-card line. Keep the first item, matching the former
    # fresh-item loop, and compute each key once.
    keyed_items: dict[str, Item] = {}
    for item in items:
        keyed_items.setdefault(monitor.key(item), item)
    current_keys = set(keyed_items)

    if is_first_run:
        return current_keys

    fresh_keys = current_keys - previous_keys
    fresh = [item for key, item in keyed_items.items() if key in fresh_keys]

    if not fresh:
        return current_keys

    try:
        messages = await monitor.render(fresh)
    except Exception as err:  # an app-side failure, not a poll failure
        # The fetch succeeded; only the rendering failed. Letting this propagate
        # would escalate backoff and eventually post "No successful poll since …
        # the monitor may be blocked or down" — untrue, and precisely the misleading
        # ops message the post-failure handling exists to avoid. So it is treated
        # exactly like an undelivered post: withhold, keep the cadence, retry.
        current_keys -= fresh_keys
        log(f"render failed ({err}); will retry {len(fresh_keys)} item(s) next tick")
        return current_keys

    settled: set[str] = set()
    announced: set[str] = set()
    posted = 0
    for index, message in enumerate(messages):
        # Not before the first: the overwhelmingly common tick has exactly one item,
        # and it should reach the phone as fast as it always did.
        if index > 0:
            await sleep(cfg.post_spacing_sec)

        try:
            await poster(cfg.alert_webhook_url, message.payload)
        except DiscordPostError as err:
            if err.status_code in PAYLOAD_REJECTED_STATUS:
                # The payload itself is rejected. Retrying is not caution, it is a way
                # of never noticing: the keys would be withheld every tick forever
                # while the heartbeat kept saying "still watching". Bank them to stop
                # the loop, and say out loud that these items are gone.
                settled.update(message.covers)
                log(
                    f"alert permanently rejected ({err}); {len(message.covers)} "
                    f"item(s) recorded as seen and will NOT be announced"
                )
                await post_status(
                    format_delivery_failure(cfg.labels, err.status_code or 0, len(message.covers))
                )
                continue

            if not err.retryable:
                # Not retryable, but the payload was fine — the webhook itself was
                # refused (401/403/404 after a rotation or deletion). Withholding is
                # right: nothing is lost once someone fixes the webhook, whereas banking
                # would discard every slot while the ops message reporting it went to the
                # same dead endpoint.
                log(
                    f"webhook refused ({err}); withholding {len(message.covers)} item(s) "
                    f"— check the webhook is still valid, nothing will be announced until it is"
                )
                continue

            log(f"alert post failed ({err}); will retry {len(message.covers)} item(s) next tick")
            if err.status_code == 429:
                # Rate-limited. Continuing the batch at post_spacing_sec would be
                # 2.86 req/s into a webhook that just said stop; the rest of the
                # batch is left unsettled and therefore retried next tick.
                remaining = len(messages) - index - 1
                log(f"rate limited — abandoning the remaining {remaining} message(s)")
                break
            continue
        except Exception as err:  # a transport fault, treated as retryable
            log(f"alert post failed ({err}); will retry {len(message.covers)} item(s) next tick")
            continue

        settled.update(message.covers)
        announced.update(message.covers)
        posted += 1

    # Any fresh key that no message settled was never announced. That includes keys
    # `render` simply omitted from its messages — banking those would swallow the
    # item silently and forever, which is the one outcome this design trades
    # everything else against.
    attempted = {key for message in messages for key in message.covers}
    uncovered = fresh_keys - attempted
    if uncovered:
        # Not a post failure, so nothing above has logged it. An app whose render
        # drops items would otherwise retry them every tick in total silence.
        log(
            f"render returned no message covering {len(uncovered)} item(s); "
            f"withholding them — this repeats every tick until render covers them"
        )
    withheld = fresh_keys - settled
    if withheld:
        current_keys -= withheld
    if announced:
        log(f"posted {posted} message(s) covering {len(announced)} new item(s)")

    return current_keys


async def run_liveness(
    cfg: RunnerConfig,
    health: HealthState,
    *,
    outcome: Literal["success", "busy", "failure"],
    items_tracked: int,
    now: int,
    heartbeat_extras: Callable[[], HeartbeatExtras],
    poster: Poster,
    log: Callable[[str], None],
) -> HealthState:
    """Fold a tick outcome into the health state and emit liveness ops messages.

    `busy` is a failure that the source *answered*: the account is in use elsewhere.
    It counts toward the stall clock exactly like `failure` — an absence of data is an
    absence of data — but while an absence is busy-only it is judged against
    `busy_stall_alert_sec` and reported with the cause rather than as a possible outage.

    Returns the next state. Every post here is best-effort and cannot escape: an ops
    message is never worth disturbing the poll loop, and an exception on the way out
    would leave `death_alerted` unset and re-post the death alert every tick forever.
    """

    async def post_status(payload: Payload) -> None:
        try:
            await poster(cfg.status_url, payload)
        except Exception as err:  # an ops message is never worth a crash
            log(f"status post failed (ignored): {err}")

    def extras() -> HeartbeatExtras:
        # App code, called from inside the loop that reports the app is alive. A
        # raise here must not be the thing that kills the monitor.
        try:
            return heartbeat_extras()
        except Exception as err:  # degrade the heartbeat, never the loop
            log(f"heartbeat_extras() raised ({err}); sending a plain heartbeat")
            return HeartbeatExtras()

    updated = health

    if outcome == "success":
        updated = replace(
            updated,
            last_success_unix=now,
            consecutive_failures=0,
            items_tracked=items_tracked,
            busy_only=False,
        )
        if updated.death_alerted:
            await post_status(format_status_alert("recovery", cfg.labels, updated, now))
            # The recovery message itself signals liveness, so restart the heartbeat
            # cadence from here. Otherwise a heartbeat that came due *during* the
            # outage (last_heartbeat_unix was frozen while latched-dead) would fire
            # immediately after recovery, doubling up on the "I'm alive" signal.
            updated = replace(updated, death_alerted=False, last_heartbeat_unix=now)
    else:
        # `busy_only` means "every failure since the last success was a busy signal".
        # Read before the increment below, so consecutive_failures == 0 identifies the
        # first failure of this absence — which is what *starts* the streak, since a
        # success clears the flag. A later busy tick only continues a streak that is
        # already busy-only, and any non-busy outcome ends it until the next success.
        # That asymmetry is the whole mechanism: grace is granted while nothing but
        # collisions happen, and revoked permanently by the first real fault.
        starts_the_absence = updated.consecutive_failures == 0
        busy_only = outcome == "busy" and (starts_the_absence or updated.busy_only)
        updated = replace(
            updated,
            consecutive_failures=updated.consecutive_failures + 1,
            busy_only=busy_only,
        )
        if (
            should_alert_stall(updated, now, cfg.stall_alert_sec, cfg.busy_stall_alert_sec)
            and not updated.death_alerted
        ):
            # One latch for both wordings: a busy-only absence that later turns into a
            # real fault has already said something, and re-alerting to change the
            # explanation would be two messages about one outage.
            await post_status(
                format_status_alert("busy" if busy_only else "death", cfg.labels, updated, now)
            )
            updated = replace(updated, death_alerted=True)

    # Suppress the heartbeat while latched-dead: the death alert already signals
    # liveness, and a "still watching" message mid-outage would contradict it.
    if not updated.death_alerted and should_heartbeat(
        updated, now, cfg.heartbeat_interval_sec, cfg.heartbeat_at
    ):
        # extras() is called here and nowhere else: only when a heartbeat is actually
        # due, so an app doing real work to assemble it does not do it every tick.
        await post_status(format_heartbeat(cfg.labels, updated, now, extras()))
        updated = replace(updated, last_heartbeat_unix=now)

    return updated


def _format_at(heartbeat_at: tuple[int, int] | None) -> str:
    return "off" if heartbeat_at is None else f"{heartbeat_at[0]:02d}:{heartbeat_at[1]:02d}"


def install_shutdown_handlers(log: Callable[[str], None]) -> None:
    """Log and exit on SIGTERM/SIGINT. Called by the app's `main`, which owns the
    process — the library is a loop, not a process supervisor.

    Exits immediately rather than draining the current tick. A kill between the post
    and the save re-alerts on restart, which is the trade this whole design makes on
    purpose; waiting for the tick to finish would only delay SIGTERM until Docker's
    kill timeout, and `save_state`'s temp-and-rename means no kill can leave a
    half-written baseline either way.
    """

    def handle(sig: signal.Signals) -> None:
        log(f"{sig.name} received — shutting down")
        raise SystemExit(0)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, functools.partial(handle, sig))


async def run_forever[Item](
    monitor: Monitor[Item],
    cfg: RunnerConfig,
    *,
    poster: Poster,
    now_unix: Callable[[], int] = system_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
    log: Callable[[str], None] | None = None,
    max_ticks: int | None = None,
) -> None:
    """Own the loop: load state, poll, alert, persist, report, sleep.

    `max_ticks` exists so the loop's own wiring — the save, the health folding, the
    choice between jitter and backoff — is testable. Production passes None.
    """
    emit = make_log(cfg.log_prefix) if log is None else log

    loaded = load_state(cfg.state_path)
    first_run = loaded.keys is None
    if loaded.corrupt:
        # Missing and corrupt both re-baseline, and they mean opposite things.
        # Nothing can recover the lost keys, so the only useful response is to say so.
        emit(
            f"{cfg.state_path} could not be read as a key array — "
            f"re-baselining silently; anything open right now will not be announced"
        )

    health = init_health(now_unix())
    emit(
        f"started — interval={cfg.poll_interval_sec}s jitter={cfg.poll_jitter_pct}% "
        f"firstRun={first_run} "
        f"statusChannel={'separate' if cfg.status_webhook_url else 'main'} "
        f"heartbeat={cfg.heartbeat_interval_sec}s "
        f"heartbeatAt={_format_at(cfg.heartbeat_at)} stall={cfg.stall_alert_sec}s "
        # busyStall governs jeffco's single commonest failure -- the account being
        # in use by its owner -- and deciding whether a quiet hour is expected or
        # alarming needs the threshold that was actually in force. It is not wired
        # into Terraform on purpose, which makes the log the only place to see it.
        f"busyStall={cfg.busy_stall_alert_sec}s"
    )

    async def post_status(payload: Payload) -> None:
        # run_tick's channel for a permanent delivery failure. Swallows its own
        # errors so an ops message can never disturb polling; run_liveness has the
        # same guard for the same reason.
        try:
            await poster(cfg.status_url, payload)
        except Exception as err:  # an ops message is never worth a crash
            emit(f"status post failed (ignored): {err}")

    previous_keys = loaded.keys or set()
    backoff = 0.0
    ticks = 0

    while max_ticks is None or ticks < max_ticks:
        ticks += 1
        tick_unix = now_unix()
        outcome: Literal["success", "busy", "failure"]
        items_tracked = health.items_tracked
        try:
            current_keys = await run_tick(
                monitor,
                cfg,
                previous_keys,
                is_first_run=first_run,
                poster=poster,
                post_status=post_status,
                sleep=sleep,
                log=emit,
            )
        except SourceBusy as err:
            # A busy signal is not a fault, so it must not compound like one. Hold
            # the normal cadence and leave the escalation ladder where it was, so a
            # real fault arriving later still climbs from where it left off.
            delay = with_jitter(cfg.poll_interval_sec, cfg.poll_jitter_pct, rand)
            emit(f"tick failed (source busy elsewhere), retrying in {round(delay)}s: {err}")
            outcome = "busy"
        except Exception as err:  # one response to every fault
            backoff = next_backoff(backoff, cfg.poll_interval_sec, cfg.max_backoff_sec)
            emit(f"tick failed, backing off {backoff}s: {err}")
            delay = backoff
            outcome = "failure"
        else:
            # The in-memory baseline advances *before* the write is attempted. A
            # persistently unwritable state_path would otherwise re-alert the same items
            # every tick, forever. `first_run` moves for the same reason and matters
            # more: stuck at true, a genuinely new item on a later tick would be
            # silently swallowed.
            previous_keys = current_keys
            first_run = False

            try:
                save_state(cfg.state_path, current_keys)
            except OSError as err:
                # Durability is best-effort. Reporting this as a *poll* failure would
                # throttle polling, latch a false death alert, and suppress the
                # heartbeat, all while alerts were arriving normally.
                emit(
                    f"state write to {cfg.state_path} failed ({err}); continuing on the "
                    f"in-memory baseline — a restart will re-baseline and silently "
                    f"suppress everything currently open, so fix this"
                )

            backoff = 0.0
            delay = with_jitter(cfg.poll_interval_sec, cfg.poll_jitter_pct, rand)
            outcome = "success"
            items_tracked = len(current_keys)

        health = await run_liveness(
            cfg,
            health,
            outcome=outcome,
            items_tracked=items_tracked,
            now=tick_unix,
            heartbeat_extras=monitor.heartbeat_extras,
            poster=poster,
            log=emit,
        )
        await sleep(delay)
