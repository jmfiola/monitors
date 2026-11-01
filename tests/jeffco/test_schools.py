import json
from pathlib import Path
from typing import Any

import pytest
from jeffco.schools import (
    DEFAULT_HS_SCHOOLS,
    is_high_school,
    normalize_school_name,
    parse_school_list,
    partition_jobs,
)
from jeffco.types import Job

ALLOW = parse_school_list("\n".join(DEFAULT_HS_SCHOOLS))
NONE: set[str] = set()

# The campuses whose names say nothing about grade level. Everything else in
# the built-in list is reachable by the pattern alone -- which is the property
# worth testing, because it is what covers schools nobody enumerated.
LIST_ONLY = ["DORAL ACADEMY OF COLORADO", "WARREN TECH NORTH", "JEFFERSON ACADEMY"]
PATTERN_COVERED = [n for n in DEFAULT_HS_SCHOOLS if n not in LIST_ONLY]

FIXTURE: Any = json.loads(
    (Path(__file__).parent / "fixtures" / "available-jobs.json").read_text(encoding="utf-8")
)


def job(location: str, job_id: int = 1) -> Job:
    return Job(
        job_id=job_id,
        location_name=location,
        job_start="2026-10-16T13:45Z",
        job_end="2026-10-16T20:30Z",
    )


def _job_from_fixture(entry: dict[str, Any]) -> Job:
    return Job(
        job_id=entry["jobId"],
        location_name=entry["locationName"],
        job_start=entry["jobStart"],
        job_end=entry["jobEnd"],
        classf_name=entry.get("classfName"),
        employee_first_name=entry.get("employeeFirstName"),
        employee_last_name=entry.get("employeeLastName"),
        days_of_week=entry.get("daysOfWeek"),
        duration_type=entry.get("durationType"),
        job_status=entry.get("jobStatus"),
    )


AVAILABLE_JOBS = [_job_from_fixture(entry) for entry in FIXTURE]


# --- normalize_school_name --------------------------------------------------


def test_folds_senior_high_school_and_sr_high_to_the_hs_token() -> None:
    # The remaining fold-table entries exercised by A1 that the roster/ordering
    # examples below don't cover on their own.
    assert normalize_school_name("Golden Senior High School") == "GOLDEN HS"
    assert normalize_school_name("Golden Sr High") == "GOLDEN HS"


def test_turns_punctuation_into_word_boundaries_and_collapses_whitespace() -> None:
    assert normalize_school_name("  Pomona   Junior/Senior ") == "POMONA JUNIOR SENIOR"
    assert normalize_school_name("Alameda International Jr/Sr") == "ALAMEDA INTERNATIONAL JR SR"
    assert normalize_school_name("Arvada K-8") == "ARVADA K 8"


def test_preserves_the_apostrophe_so_both_sides_of_a_comparison_agree() -> None:
    assert normalize_school_name("D'Evelyn Junior/Senior") == "D'EVELYN JUNIOR SENIOR"
    assert normalize_school_name("D'EVELYN JUNIOR/SENIOR") == "D'EVELYN JUNIOR SENIOR"


def test_does_not_fold_junior_high_which_would_make_every_middle_school_a_match() -> None:
    # Folding JUNIOR HIGH, or treating bare JR as a token, would make every
    # middle school a match.
    assert normalize_school_name("Drake Junior High") == "DRAKE JUNIOR HIGH"
    assert normalize_school_name("Jefferson Academy Jr High") == "JEFFERSON ACADEMY JR HIGH"


def test_a_roster_spelling_matches_sfes_spelling() -> None:
    # The whole point of normalization: the district roster says "Ralston Valley
    # High School" and SFE says "RALSTON VALLEY HS".
    assert normalize_school_name("Ralston Valley High School") == normalize_school_name(
        "RALSTON VALLEY HS"
    )


def test_the_longest_fold_wins_so_no_stray_senior_survives() -> None:
    # FOLDS is ordered longest-first specifically so "SENIOR HIGH SCHOOL" collapses
    # in one step instead of leaving a bare SENIOR that the pattern would then match.
    assert normalize_school_name("Foo Senior High School") == "FOO HS"


def test_the_apostrophe_survives_because_both_sides_run_the_same_folds() -> None:
    assert is_high_school("D'Evelyn Junior/Senior", set()) is True


def test_junior_high_deliberately_does_not_match() -> None:
    # Folding JUNIOR HIGH, or treating bare JR as a token, would make every middle
    # school a high school.
    assert is_high_school("SOMEWHERE JUNIOR HIGH", set()) is False


# --- is_high_school -- the real school list ---------------------------------


@pytest.mark.parametrize("name", PATTERN_COVERED)
def test_matches_with_the_list_emptied(name: str) -> None:
    assert is_high_school(name, NONE) is True


def test_matches_a_high_school_nobody_enumerated() -> None:
    # The whole point of the pattern. If this breaks, every Jeffco high school
    # outside the hardcoded names goes silently unalerted.
    assert is_high_school("SOMEWHERE NEW HIGH SCHOOL", NONE) is True
    assert is_high_school("Somewhere New Sr High", NONE) is True


def test_matches_sfes_bare_spelling_of_the_jefferson_academy_campus() -> None:
    # Observed in production: SFE sends a bare "JEFFERSON ACADEMY", which the
    # pattern cannot reach (no HS / SENIOR / JR SR token) and which is not the
    # same string as the listed "JEFFERSON ACADEMY SENIOR". The account holder
    # works at this campus, so before it was listed his jobs there were dropped
    # and surfaced only as a name in the heartbeat's gap report.
    assert is_high_school("JEFFERSON ACADEMY", NONE) is False
    assert is_high_school("JEFFERSON ACADEMY", ALLOW) is True


def test_matches_the_two_schools_confirmed_against_the_live_api() -> None:
    assert is_high_school("RALSTON VALLEY HS", ALLOW) is True
    assert is_high_school("WHEAT RIDGE HIGH SCHOOL", ALLOW) is True


def test_matches_a_district_roster_spelling_against_sfes_different_spelling() -> None:
    # The roster says "High School", SFE says "HS". Normalization is what bridges them.
    assert is_high_school("Ralston Valley High School", ALLOW) is True


def test_has_no_duplicate_entries_once_normalized() -> None:
    # A duplicate would mean two spellings of one campus, i.e. a stale roster entry.
    assert len({normalize_school_name(n) for n in DEFAULT_HS_SCHOOLS}) == len(DEFAULT_HS_SCHOOLS)


# --- is_high_school -- what the pattern alone can and cannot do -------------


def test_catches_combined_and_option_campuses_with_no_list_at_all() -> None:
    # These justify the SENIOR and JR SR tokens: they generalize to campuses
    # nobody enumerated.
    assert is_high_school("POMONA JUNIOR/SENIOR", NONE) is True
    assert is_high_school("JEFFCO OPEN SENIOR", NONE) is True
    assert is_high_school("ALAMEDA INTERNATIONAL JR/SR", NONE) is True


def test_is_blind_to_the_two_list_only_schools_which_is_the_residual_risk() -> None:
    # Nothing in these names says "high school". If a campus like these is not
    # on the list, it is missed until gap reporting surfaces it.
    assert is_high_school("DORAL ACADEMY OF COLORADO", NONE) is False
    assert is_high_school("WARREN TECH NORTH", NONE) is False
    assert is_high_school("DORAL ACADEMY OF COLORADO", ALLOW) is True
    assert is_high_school("WARREN TECH NORTH", ALLOW) is True


def test_an_unrecognizable_name_needs_the_allow_list() -> None:
    # DORAL ACADEMY and WARREN TECH say nothing about grade level. This is exactly
    # what the gap report exists to surface.
    assert is_high_school("DORAL ACADEMY OF COLORADO", set()) is False
    assert (
        is_high_school("DORAL ACADEMY OF COLORADO", parse_school_list("Doral Academy of Colorado"))
        is True
    )


# --- is_high_school -- negatives ---------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "FOSTER DUAL LANGUAGE PK-8",  # from the live sample
        "CREIGHTON MIDDLE SCHOOL",
        "DRAKE JUNIOR HIGH",
        "JEFFERSON ACADEMY JR HIGH",  # junior-high sibling of a listed senior campus
        "ARVADA K-8",
    ],
)
def test_excludes_non_high_schools(name: str) -> None:
    assert is_high_school(name, ALLOW) is False


# --- parse_school_list -------------------------------------------------------


def test_splits_on_newlines_and_semicolons_normalizes_and_drops_blanks() -> None:
    assert parse_school_list("Arvada High School\n\nGolden HS ; Conifer High School") == {
        "ARVADA HS",
        "GOLDEN HS",
        "CONIFER HS",
    }


def test_returns_an_empty_set_for_an_empty_string() -> None:
    assert parse_school_list("") == set()


# --- partition_jobs -----------------------------------------------------------


def test_splits_hs_from_the_rest_and_collects_unmatched_names_for_gap_reporting() -> None:
    jobs = [
        job("RALSTON VALLEY HS", 1),
        job("FOSTER DUAL LANGUAGE PK-8", 2),
        job("WHEAT RIDGE HIGH SCHOOL", 3),
        job("SOME NEW ACADEMY", 4),
    ]
    hs, unmatched = partition_jobs(jobs, ALLOW)
    assert [j.job_id for j in hs] == [1, 3]
    assert unmatched == ["FOSTER DUAL LANGUAGE PK-8", "SOME NEW ACADEMY"]


def test_reports_each_unmatched_name_once_with_the_raw_spelling_for_pasting_into_config() -> None:
    jobs = [job("Foster Dual Language PK-8", 1), job("Foster Dual Language PK-8", 2)]
    _, unmatched = partition_jobs(jobs, ALLOW)
    assert unmatched == ["Foster Dual Language PK-8"]


def test_keeps_the_three_hs_rows_from_the_real_captured_list() -> None:
    # Drives the filter from the real fixture rather than hand-rolled strings, so a
    # regenerated fixture with a renamed school fails here instead of going quiet.
    hs, unmatched = partition_jobs(AVAILABLE_JOBS, ALLOW)
    assert [j.job_id for j in hs] == [1025548, 1025533, 1025535]
    assert unmatched == ["FOSTER DUAL LANGUAGE PK-8"]


def test_partition_returns_raw_unmatched_spellings_sorted_and_deduplicated() -> None:
    # Raw, so they can be pasted into HS_SCHOOLS verbatim; deduplicated because the
    # list accumulates across a process lifetime and the heartbeat renders it.
    hs, unmatched = partition_jobs(
        [
            job("LITTLE ELE"),
            job("GOLDEN HIGH SCHOOL", 2),
            job("LITTLE ELE", 3),
            job("Bar Middle", 4),
        ],
        parse_school_list("\n".join(DEFAULT_HS_SCHOOLS)),
    )
    assert [j.job_id for j in hs] == [2]
    assert unmatched == ["Bar Middle", "LITTLE ELE"]


# The schools whose names say nothing about grade level. This is the entire
# justification for the allow-list existing alongside the pattern — and it is a real
# assertion, unlike checking the shipped list against an allow-list derived from
# itself, which is true by construction and passes even with the pattern destroyed.
#
# JEFFERSON ACADEMY is the one that arrived from production rather than from a
# roster: SFE sends it bare, so neither the pattern nor the longer "JEFFERSON
# ACADEMY SENIOR" entry reached it and the account holder's jobs there were dropped.
NEEDS_THE_ALLOW_LIST = {
    "DORAL ACADEMY OF COLORADO",
    "WARREN TECH NORTH",
    "JEFFERSON ACADEMY",
}


def test_exactly_the_shipped_pattern_blind_schools_need_the_allow_list() -> None:
    by_pattern = {n for n in DEFAULT_HS_SCHOOLS if is_high_school(n, set())}
    needs_list = set(DEFAULT_HS_SCHOOLS) - by_pattern
    assert needs_list == NEEDS_THE_ALLOW_LIST
    # And the list genuinely rescues them.
    allow = parse_school_list("\n".join(DEFAULT_HS_SCHOOLS))
    for name in needs_list:
        assert is_high_school(name, allow) is True
