"""The poll loop. The contract an app implements to be driven by it is in types.py."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from monitor.config import RunnerConfig
from monitor.discord import (
    DiscordPostError,
    Poster,
    StatusPoster,
    format_delivery_failure,
)
from monitor.types import Monitor


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
    current_keys = {monitor.key(item) for item in items}

    if is_first_run:
        return current_keys

    # De-duplicated by key, which the TypeScript does not do. melanzana's month
    # enumeration pads and therefore overlaps, so one slot can arrive twice in a
    # single tick — and a duplicate would inflate the alert's own "N open slot(s)"
    # count and repeat its day-card line. The baseline was always a set and so was
    # never affected; only the rendered message was.
    fresh: list[Item] = []
    fresh_keys: set[str] = set()
    for item in items:
        key = monitor.key(item)
        if key in previous_keys or key in fresh_keys:
            continue
        fresh_keys.add(key)
        fresh.append(item)

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
            if not err.retryable:
                # Permanent. Retrying is not caution, it is a way of never noticing:
                # the keys would be withheld every tick forever while the heartbeat
                # kept saying "still watching". Bank them to stop the loop, and say
                # out loud that these items are gone.
                settled.update(message.covers)
                log(
                    f"alert permanently rejected ({err}); {len(message.covers)} "
                    f"item(s) recorded as seen and will NOT be announced"
                )
                await post_status(
                    format_delivery_failure(cfg.labels, err.status_code or 0, len(message.covers))
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
