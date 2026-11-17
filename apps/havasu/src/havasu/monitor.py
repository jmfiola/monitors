"""The four contract methods. Everything the library does not own lives here."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx
from monitor.timing import system_now
from monitor.types import Field, HeartbeatExtras, Message, Monitor

from havasu.alert import format_alert
from havasu.config import HavasuConfig, describe_ranges
from havasu.detector import open_arrivals, query_window
from havasu.newbook import NewBookSource
from havasu.types import OpenArrival

#: Supai keeps Arizona time — no DST — and "today" decides which arrivals are past.
SITE_TZ = ZoneInfo("America/Phoenix")


class HavasuMonitor:
    """Implements `monitor.runner.Monitor[OpenArrival]`.

    The only state between ticks is the NewBook session, held by the source.
    """

    def __init__(
        self,
        cfg: HavasuConfig,
        client: httpx.AsyncClient,
        now_unix: Callable[[], int] = system_now,
    ) -> None:
        self._cfg = cfg
        self._source = NewBookSource(client)
        self._now_unix = now_unix

    async def fetch(self) -> list[OpenArrival]:
        """Every watched arrival date that can be booked right now.

        One calendar request covers every watched month. Any failure propagates —
        no data is not the same as nothing open.
        """
        today = datetime.fromtimestamp(self._now_unix(), SITE_TZ).date()
        first, last = query_window(self._cfg.watch_ranges, today)
        days = await self._source.fetch_calendar(
            first, last, available_from=max(today, first.first_day())
        )
        return open_arrivals(days, self._cfg.watch_ranges, today)

    def key(self, item: OpenArrival) -> str:
        """The arrival date. A date that sells out and reopens drops out of the
        baseline in between, so its reopening is fresh again."""
        return f"campground:{item.arrival.isoformat()}"

    async def render(self, new: list[OpenArrival]) -> list[Message]:
        """One message covering every fresh key. Nothing to fetch, so never awaits."""
        return [
            Message(
                payload=format_alert(new, booking_url=self._cfg.booking_url),
                covers=tuple(self.key(a) for a in new),
            )
        ]

    def heartbeat_extras(self) -> HeartbeatExtras:
        """Which arrival ranges are watched, so a test config left on is visible."""
        return HeartbeatExtras(
            fields=(
                Field(
                    name="Watching arrivals",
                    value=describe_ranges(self._cfg.watch_ranges),
                    inline=False,
                ),
            )
        )


if TYPE_CHECKING:  # a compile-time assertion, no runtime cost, no fake arguments

    def _assert_satisfies_protocol(m: HavasuMonitor) -> Monitor[OpenArrival]:
        return m
