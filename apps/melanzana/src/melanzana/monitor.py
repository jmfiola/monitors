"""The four contract methods. Everything the library does not own lives here."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx
from monitor.timing import system_now
from monitor.types import HeartbeatExtras, Message, Monitor

from melanzana.alert import format_alert
from melanzana.config import MelanzanaConfig
from melanzana.cowlendar import fetch_month
from melanzana.detector import filter_bookable, months_to_fetch
from melanzana.types import Slot


class MelanzanaMonitor:
    """Implements `monitor.runner.Monitor[Slot]`.

    Holds no state between ticks — melanzana has none to hold. The class shape comes
    from jeffco, which caches a token and accumulates filter gaps.
    """

    def __init__(
        self,
        cfg: MelanzanaConfig,
        client: httpx.AsyncClient,
        now_unix: Callable[[], int] = system_now,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._now_unix = now_unix

    async def fetch(self) -> list[Slot]:
        """Every bookable slot inside the window, right now.

        One `now` for the whole tick: month selection and the window cut must agree,
        and a clock read between them could disagree at a boundary.

        Months are fetched concurrently. A failure in any one of them propagates —
        no data is not the same as "nothing available", and treating a partial
        result as complete would bank the missing slots as gone and re-alert them
        all on the next success.
        """
        now = self._now_unix()
        months = months_to_fetch(now, self._cfg.window_days)
        fetched = await asyncio.gather(
            *(
                fetch_month(
                    self._cfg.calendar_id,
                    self._cfg.variant_id,
                    self._cfg.timezone,
                    month.year,
                    month.month,
                    self._client,
                )
                for month in months
            )
        )
        slots = [slot for month_slots in fetched for slot in month_slots]
        return filter_bookable(slots, now, self._cfg.window_days)

    def key(self, item: Slot) -> str:
        """The API's own slot string. Identity and display are the same value."""
        return item.key

    async def render(self, new: list[Slot]) -> list[Message]:
        """One message covering every fresh key.

        Async because the contract is — jeffco enriches per item here. Melanzana has
        nothing to fetch, so this never awaits.
        """
        return [
            Message(
                payload=format_alert(
                    new,
                    booking_url=self._cfg.booking_url,
                    mention_everyone=self._cfg.mention_everyone,
                ),
                covers=tuple(slot.key for slot in new),
            )
        ]

    def heartbeat_extras(self) -> HeartbeatExtras:
        """Nothing to add. Jeffco reports unrecognised school names here, and
        replaces the footer with what to do about them."""
        return HeartbeatExtras()


if TYPE_CHECKING:  # a compile-time assertion, no runtime cost, no fake arguments

    def _assert_satisfies_protocol(m: MelanzanaMonitor) -> Monitor[Slot]:
        return m
