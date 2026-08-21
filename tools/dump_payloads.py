"""Dump every Discord payload melanzana can produce, as canonical JSON.

Half of the differential harness. The TypeScript half lives at
`melanzana-monitor/tools/dump-payloads.ts` and must print byte-identical output.

Both sides pin the clock and build the synthetic cases from the same literals, so
any difference in the output is a difference in the *implementations*. Verified
during planning: Node's JSON.stringify(canon(x), null, 2) and this
json.dumps(..., sort_keys=True, indent=2, ensure_ascii=False) agree byte for byte.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from melanzana.alert import format_alert
from melanzana.config import LABELS
from melanzana.cowlendar import parse_availability
from melanzana.detector import filter_bookable
from melanzana.types import Slot
from monitor.discord import format_heartbeat, format_status_alert
from monitor.health import init_health

#: Frozen clock. Month selection and weekday rendering must be deterministic rather
#: than passing until the first of the month.
NOW = 1796000000
WINDOW_DAYS = 60
BOOKING_URL = "https://melanzana.com/pages/how-to-shop"
FIXTURE = Path(__file__).resolve().parents[1] / "tests/melanzana/fixtures/availability-sample.json"


def _slot(key: str, qty_left: int) -> Slot:
    # start_unix is inside the window for every synthetic case; these cases exercise
    # rendering, and the window cut has its own tests.
    return Slot(key=key, start_unix=1796146200, qty_left=qty_left, is_bookable=True)


def build(mention: bool) -> dict[str, Any]:
    fixture_slots = filter_bookable(
        parse_availability(json.loads(FIXTURE.read_text(encoding="utf-8"))), NOW, WINDOW_DAYS
    )

    multi_day = [
        _slot("2026-12-02 09:00", 5),
        _slot("2026-12-01 11:00", 2),
        _slot("2026-12-01 10:30", 4),
    ]
    overflow = [_slot(f"2026-09-{day:02d} 10:00", 4) for day in range(1, 31)]
    unparseable = [_slot("weird-key", 4)]
    # A parseable key and a raw one in the same batch — the only place `localeCompare`
    # and Python's code-point `sorted` could disagree, since ICU can treat punctuation
    # as ignorable. Checked against node during planning and they agree on every
    # realistic pair, including "weird-key" vs "2026-12-01" and "a-b" vs "ab"; this
    # case exists so the assumption is pinned rather than remembered.
    mixed = [_slot("weird-key", 1), _slot("2026-12-01 10:30", 4), _slot("also-weird", 2)]

    heartbeat_state = replace(init_health(1000), items_tracked=7, last_success_unix=1500)
    death_state = replace(init_health(1000), last_success_unix=1000, consecutive_failures=4)

    return {
        "1-fixture": format_alert(
            fixture_slots, booking_url=BOOKING_URL, mention_everyone=mention
        ).to_dict(),
        "2-multi-day": format_alert(
            multi_day, booking_url=BOOKING_URL, mention_everyone=mention
        ).to_dict(),
        "3-overflow": format_alert(
            overflow, booking_url=BOOKING_URL, mention_everyone=mention
        ).to_dict(),
        "4-unparseable-key": format_alert(
            unparseable, booking_url=BOOKING_URL, mention_everyone=mention
        ).to_dict(),
        "5-mixed-keys": format_alert(
            mixed, booking_url=BOOKING_URL, mention_everyone=mention
        ).to_dict(),
        "6-heartbeat": format_heartbeat(LABELS, heartbeat_state, 1000 + 3600).to_dict(),
        "7-death": format_status_alert("death", LABELS, death_state, 2000).to_dict(),
        "8-recovery": format_status_alert("recovery", LABELS, init_health(1000), 2100).to_dict(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mention", choices=("true", "false"), required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.mention == "true"), sort_keys=True, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
