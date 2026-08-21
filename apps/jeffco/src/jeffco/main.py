"""Wire a JeffcoMonitor to the shared runner."""

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

from jeffco.config import LOG_PREFIX, load_config
from jeffco.monitor import JeffcoMonitor
from jeffco.sfe import SfeClient

#: Generous but finite. Without a timeout a hung SFE connection stalls the loop
#: indefinitely, which looks identical to "nothing new".
HTTP_TIMEOUT_SEC = 20.0

log = make_log(LOG_PREFIX)


async def _main() -> None:
    cfg = load_config(os.environ)
    # Deliberately excludes sfe_user_id: this account's PIN closely resembles its
    # access id, so printing the id would leak most of the PIN's shape into logs
    # that are not a secret store. `len(cfg.hs_schools)`, not the schools
    # themselves -- the list is long and not worth a log line of its own.
    log(f"app config — window={cfg.window_days}d tz={cfg.timezone} schools={len(cfg.hs_schools)}")

    # This process's owner installs its own signal handlers; the library is a
    # loop, not a supervisor.
    install_shutdown_handlers(log)

    async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT_SEC)) as client:
        sfe = SfeClient(
            client=client,
            user_id=cfg.sfe_user_id,
            pin=cfg.sfe_pin,
            timezone=cfg.timezone,
            window_days=cfg.window_days,
            now_unix=system_now,
            log=log,
        )

        # ONE clock, passed to both the monitor and the runner below. Nothing
        # inside JeffcoMonitor reads it today -- every SFE call carries its own --
        # but a test that froze one clock and not the other would produce results
        # that look inexplicable, so the convention is kept even where it is not
        # yet load-bearing.
        monitor = JeffcoMonitor(cfg, sfe, log=log, now_unix=system_now)

        async def poster(url: str, payload: Payload) -> None:
            await post(url, payload, client)

        await run_forever(monitor, cfg.runner, poster=poster, now_unix=system_now, log=log)


def main() -> None:
    try:
        asyncio.run(_main())
    except SystemExit:
        raise
    except BaseException as err:
        # The message, not the exception object: a repr can carry a URL or a
        # session id, and both are credentials.
        print(f"[{LOG_PREFIX}] fatal: {err}", file=sys.stderr, flush=True)
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
