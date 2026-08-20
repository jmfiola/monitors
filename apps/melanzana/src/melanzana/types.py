"""Melanzana's item type. The library never inspects it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Slot:
    """A single appointment slot, normalized from the Cowlendar API."""

    #: The API `slot` string, e.g. "2026-12-01 10:30". Identity key AND display.
    key: str
    #: The API `slot_start_unix`, epoch seconds. Used for all window/time math.
    start_unix: int
    #: The API `qty_left` — spots remaining.
    qty_left: int
    #: The API `is_bookable`.
    is_bookable: bool
