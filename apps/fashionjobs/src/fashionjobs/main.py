"""Wire the FashionJobs monitor to the shared runner."""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
from monitor.config import make_log
from monitor.discord import post
from monitor.runner import install_shutdown_handlers, run_forever
from monitor.state import load_state
from monitor.timing import system_now
from monitor.types import Payload

from fashionjobs.config import LOG_PREFIX, load_config
from fashionjobs.monitor import FashionJobsMonitor
from fashionjobs.site import FashionJobsSource

HTTP_TIMEOUT_SEC = 20.0

log = make_log(LOG_PREFIX)


async def _main() -> None:
    cfg = load_config(os.environ)
    log(
        "app config — filter=Stage country=France keywords=none "
        f"interval={cfg.runner.poll_interval_sec}s"
    )

    install_shutdown_handlers(log)

    loaded = load_state(cfg.runner.state_path)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_TIMEOUT_SEC),
        follow_redirects=False,
        headers={
            "Accept": "text/html",
            "User-Agent": "fashionjobs-monitor/2.0",
        },
    ) as client:
        source = FashionJobsSource(client, initial_keys=loaded.keys, log=log)
        monitor = FashionJobsMonitor(source)

        async def poster(url: str, payload: Payload) -> None:
            await post(url, payload, client)

        await run_forever(monitor, cfg.runner, poster=poster, now_unix=system_now, log=log)


def main() -> None:
    try:
        asyncio.run(_main())
    except SystemExit:
        raise
    except BaseException as err:
        print(f"[{LOG_PREFIX}] fatal: {err}", file=sys.stderr, flush=True)
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
