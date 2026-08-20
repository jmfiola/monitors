"""Wire a MelanzanaMonitor to the shared runner."""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
from monitor.config import make_log
from monitor.discord import post
from monitor.runner import install_shutdown_handlers, run_forever
from monitor.timing import system_now
from monitor.types import Payload

from melanzana.config import LOG_PREFIX, load_config
from melanzana.monitor import MelanzanaMonitor

#: Generous but finite. Without a timeout a hung Cowlendar connection stalls the
#: loop indefinitely, which looks identical to "nothing new".
HTTP_TIMEOUT_SEC = 20.0

log = make_log(LOG_PREFIX)


async def _main() -> None:
    cfg = load_config(os.environ)
    log(
        f"app config — window={cfg.window_days}d tz={cfg.timezone} "
        f"mentionEveryone={cfg.mention_everyone} calendar={cfg.calendar_id}"
    )

    # This process's owner installs its own signal handlers; the library is a loop,
    # not a supervisor.
    install_shutdown_handlers(log)

    async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT_SEC)) as client:
        # ONE clock, passed to both. The monitor needs it for its window arithmetic
        # and the runner for its health timestamps; two independent readings are
        # harmless in production and quietly confusing in a test.
        monitor = MelanzanaMonitor(cfg, client=client, now_unix=system_now)

        async def poster(url: str, payload: Payload) -> None:
            await post(url, payload, client)

        await run_forever(monitor, cfg.runner, poster=poster, now_unix=system_now, log=log)


def main() -> None:
    try:
        asyncio.run(_main())
    except SystemExit:
        raise
    except BaseException as err:
        # The message, not the exception object: a repr can carry a URL, and a
        # webhook's path is a credential.
        print(f"[{LOG_PREFIX}] fatal: {err}", file=sys.stderr, flush=True)
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
