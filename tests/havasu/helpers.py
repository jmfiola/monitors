import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"

#: Captured 2026-11-16. March-November 2026: every day booked until Nov 16, then
#: Nov 16-30 available (Nov 16 also `closed_departure`).
CAL_2026 = "calendar-2026-03-to-2026-11.json"
#: Captured 2026-11-16. December 2026-December 2027, all booked (not on sale yet).
CAL_2027 = "calendar-2026-12-to-2027-12.json"

API_PATH = (
    "https://bookingsus.newbook.cloud/online/generated_api_files/"
    "a2c34c2a209b28633a19718581677b9e.php?"
)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(fixture_text(name))
    return loaded


def with_rows(payload: dict[str, Any], rows: str) -> dict[str, Any]:
    return {**payload, "calendar_data": {"table_rows": rows}}
