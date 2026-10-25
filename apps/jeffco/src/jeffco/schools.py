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

MOM_SCHOOLS is the second instance's list, copied from Mom's SFE profile with
its district codes alongside (again, not in the API). Her profile matches it
exactly, with no pattern, so the asymmetry above inverts for her: an entry the
feed spells differently is not rescued by a pattern, and shows up only in her
instance's filter-gap log rather than as an alert.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from enum import StrEnum

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

#: Mom's locations, verbatim from her SFE profile. The codes are hers for
#: reference only; nothing matches on them.
MOM_SCHOOLS: tuple[str, ...] = (
    "EXCEL ACADEMY",  # 155300
    "FAIRMOUNT ELE",  # 114000
    "FREMONT ELE",  # 116000
    "HACKBERRY HILL ELE",  # 118800
    "LAWRENCE ELE",  # 123000
    "LINCOLN ACADEMY",  # 155800
    "LITTLE ELE",  # 123500
    "MAPLE GROVE ELE",  # 125100
    "MEIKLEJOHN ELE",  # 126500
    "SECREST ELE",  # 140000
    "SEMPER ELE",  # 140100
    "SIERRA ELE",  # 140500
    "STOTT ELE",  # 138200
    "THREE CREEKS K-8",  # 143700
    "VAN ARSDALE ELE",  # 144200
    "VANDERHOOF ELE",  # 144000
    "WARDER ELE",  # 144800
    "WEBER ELE",  # 145200
    "WEST WOODS ELE",  # 147700
)


class SchoolMatch(StrEnum):
    """How a profile decides that a location is one of its schools."""

    #: The list UNION the high-school pattern -- see `is_high_school`.
    HIGH_SCHOOL = "high-school"
    #: The list alone. No pattern can stand in for a named set of campuses.
    NAMED = "named"


#: Longest-first so "SENIOR HIGH SCHOOL" folds in one step rather than leaving a
#: stray SENIOR behind. Both sides of every comparison run through the same
#: folds, so the exact order only has to be deterministic, not canonical.
_FOLDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bSENIOR HIGH SCHOOL\b"), "HS"),
    (re.compile(r"\bSR HIGH SCHOOL\b"), "HS"),
    (re.compile(r"\bSENIOR HIGH\b"), "HS"),
    (re.compile(r"\bSR HIGH\b"), "HS"),
    (re.compile(r"\bHIGH SCHOOL\b"), "HS"),
    # SFE abbreviates elementary campuses to ELE; a spelled-out name must land on
    # the same token. ELE is not an HS_PATTERN token, so this never makes a match
    # for the high-school filter.
    (re.compile(r"\bELEMENTARY SCHOOL\b"), "ELE"),
    (re.compile(r"\bELEMENTARY\b"), "ELE"),
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


def matches_school(raw_name: str, allow: AbstractSet[str], match: SchoolMatch) -> bool:
    """Whether a location is one of the profile's schools, by that profile's rule."""
    if match is SchoolMatch.HIGH_SCHOOL:
        return is_high_school(raw_name, allow)
    return normalize_school_name(raw_name) in allow


def partition_jobs(
    jobs: Sequence[Job],
    allow: AbstractSet[str],
    match: SchoolMatch = SchoolMatch.HIGH_SCHOOL,
) -> tuple[list[Job], list[str]]:
    """Split jobs into the profile's schools and the distinct names that matched nothing.

    The unmatched list is the gap report: it is how a school whose name says
    nothing about grade level (DORAL ACADEMY, WARREN TECH) gets discovered instead
    of silently missed. Raw spellings are returned so they can be pasted into
    DEFAULT_HS_SCHOOLS or MOM_SCHOOLS verbatim.
    """
    kept: list[Job] = []
    unmatched: set[str] = set()
    for j in jobs:
        if matches_school(j.location_name, allow, match):
            kept.append(j)
        else:
            unmatched.add(j.location_name)
    return kept, sorted(unmatched)
