"""The High-School filter: normalize a school name, then classify it.

Every Jeffco high school we can name is shipped as code rather than deployment
config so the tests exercise the real data and a fresh checkout behaves right.

Over-inclusion is free: the available-jobs endpoint only returns jobs this
substitute is qualified and assigned for, so an entry for a school that never
appears in his feed is inert — as is a misspelled one. Omitting a school costs
a real job. Add on suspicion; do not wait for confirmation.

The first 16 came from the account holder with their district codes (the codes
are not in the API — see the spec). The next 6 are the district's remaining
comprehensive high schools, spelled from general knowledge, not from SFE.

The last one is SFE's own spelling. SFE reports the Jefferson Academy campus the
account holder works at as a bare "JEFFERSON ACADEMY", which matches neither
"JEFFERSON ACADEMY SENIOR" nor the pattern — so his jobs there were filtered out
and appeared only in the heartbeat's gap report. It is listed under the spelling
the API actually sends, because that is the string the filter compares.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from collections.abc import Set as AbstractSet

from jeffco.types import Job

DEFAULT_HS_SCHOOLS: tuple[str, ...] = (
    "ARVADA HIGH SCHOOL",
    "ARVADA WEST HS",
    "CHATFIELD HIGH SCHOOL",
    "CONIFER HIGH SCHOOL",
    "D'EVELYN JUNIOR/SENIOR",
    "DORAL ACADEMY OF COLORADO",
    "EVERGREEN HIGH SCHOOL",
    "GOLDEN HIGH SCHOOL",
    "JEFFCO OPEN SENIOR",
    "JEFFERSON ACADEMY SENIOR",
    "LAKEWOOD HIGH SCHOOL",
    "POMONA JUNIOR/SENIOR",
    "RALSTON VALLEY HS",
    "STANDLEY LAKE HS",
    "WARREN TECH NORTH",
    "WHEAT RIDGE HIGH SCHOOL",
    "ALAMEDA INTERNATIONAL JR/SR",
    "BEAR CREEK HIGH SCHOOL",
    "COLUMBINE HIGH SCHOOL",
    "DAKOTA RIDGE HIGH SCHOOL",
    "GREEN MOUNTAIN HIGH SCHOOL",
    "JEFFERSON JUNIOR/SENIOR",
    "JEFFERSON ACADEMY",
)

#: Longest-first so "SENIOR HIGH SCHOOL" folds in one step rather than leaving a
#: stray SENIOR behind. Both sides of every comparison run through the same
#: folds, so the exact order only has to be deterministic, not canonical.
_FOLDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bSENIOR HIGH SCHOOL\b"), "HS"),
    (re.compile(r"\bSR HIGH SCHOOL\b"), "HS"),
    (re.compile(r"\bSENIOR HIGH\b"), "HS"),
    (re.compile(r"\bSR HIGH\b"), "HS"),
    (re.compile(r"\bHIGH SCHOOL\b"), "HS"),
)

#: JUNIOR HIGH deliberately does not fold and bare JR is deliberately not a
#: token: either would make every middle school a match. JR SR is the
#: abbreviated combined campus, which is a high school.
_HS_PATTERN = re.compile(r"(^|\s)(HS|SENIOR|JR SR)(\s|$)")

_PUNCTUATION = re.compile(r"[.,/\-]")
_WHITESPACE = re.compile(r"\s+")


def normalize_school_name(raw: str) -> str:
    """Canonical form for comparing a school name from any source.

    Applied to both config entries and an incoming location name, which is what
    lets a district-roster spelling ("Ralston Valley High School") match SFE's
    ("RALSTON VALLEY HS").

    The apostrophe in D'EVELYN is preserved rather than stripped — consistency
    across both sides is what matters, not which choice.
    """
    s = raw.upper().strip()
    s = _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", s)).strip()
    for pattern, replacement in _FOLDS:
        s = pattern.sub(replacement, s)
    return _WHITESPACE.sub(" ", s).strip()


def parse_school_list(raw: str) -> set[str]:
    """Parse a newline- or semicolon-separated config value into normalized names."""
    names = (normalize_school_name(entry) for entry in re.split(r"[;\n]", raw))
    return {name for name in names if name != ""}


def is_high_school(raw_name: str, allow: AbstractSet[str]) -> bool:
    """Union of list and pattern.

    Union rather than intersection because a miss costs a real job opportunity
    while a false positive costs one ignored Discord message — the asymmetry
    justifies being permissive. There is deliberately no denylist: the thing it
    would suppress is that one ignored message, and if the pattern ever
    over-matches, the fix is to edit the pattern, not to carry a config knob for it.
    """
    name = normalize_school_name(raw_name)
    return name in allow or _HS_PATTERN.search(name) is not None


def partition_jobs(jobs: Sequence[Job], allow: AbstractSet[str]) -> tuple[list[Job], list[str]]:
    """Split jobs into the High School ones and the distinct names that matched nothing.

    The unmatched list is the gap report: it is how a school whose name says
    nothing about grade level (DORAL ACADEMY, WARREN TECH) gets discovered instead
    of silently missed. Raw spellings are returned so they can be pasted into
    DEFAULT_HS_SCHOOLS verbatim.
    """
    hs: list[Job] = []
    unmatched: set[str] = set()
    for j in jobs:
        if is_high_school(j.location_name, allow):
            hs.append(j)
        else:
            unmatched.add(j.location_name)
    return hs, sorted(unmatched)
