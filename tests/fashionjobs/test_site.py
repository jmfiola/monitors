from datetime import datetime
from pathlib import Path

import pytest
from fashionjobs.site import STAGE_URL, FashionJobsParseError, page_url, parse_page

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_stage_route_is_fixed_and_has_no_keyword_or_location_query() -> None:
    assert STAGE_URL == "https://fr.fashionjobs.com/fr/contrat/Stage,5.html"
    assert page_url(1) == STAGE_URL
    assert page_url(2) == "https://fr.fashionjobs.com/fr/contrat/Stage,5,2.html"
    assert "?" not in STAGE_URL


def test_extracts_ordinary_and_promoted_stage_cards() -> None:
    page = parse_page(fixture("stage-page-1.html"), expected_url=STAGE_URL)

    assert [job.job_id for job in page.jobs] == [12000001, 12000002, 12000003]
    assert page.jobs[0].title == "Stage Assistant Produit"
    assert page.jobs[0].company == "MAISON EXEMPLE"
    assert page.jobs[0].location == "Paris"
    assert [job.location for job in page.jobs] == ["Paris", "Pantin", "Lyon"]
    assert page.jobs[0].contract == "Stage"
    assert page.jobs[0].published_at == datetime.fromisoformat("2026-08-21T21:50:27+02:00")
    assert page.jobs[0].url.endswith("/Stage-assistant-produit,12000001.html")
    assert page.jobs[1].url == "https://fr.fashionjobs.com/redir/12000002,1.html"
    assert page.result_count == 1266
    assert page.next_url == "https://fr.fashionjobs.com/fr/contrat/Stage,5,2.html"
    assert page.last_page == 42


def test_void_tags_inside_card_do_not_prevent_extraction() -> None:
    html = fixture("stage-page-1.html").replace(
        '<div class="job-card job-card__wrapper job-card__wrapper--col">',
        '<div class="job-card job-card__wrapper job-card__wrapper--col"><img src="logo.png"><br>',
        1,
    )

    page = parse_page(html, expected_url=STAGE_URL)

    assert [job.job_id for job in page.jobs] == [12000001, 12000002, 12000003]


def test_self_closing_void_tag_inside_card_does_not_prevent_extraction() -> None:
    html = fixture("stage-page-1.html").replace(
        '<div class="job-card job-card__wrapper job-card__wrapper--col">',
        '<div class="job-card job-card__wrapper job-card__wrapper--col"><img src="logo.png" />',
        1,
    )

    page = parse_page(html, expected_url=STAGE_URL)

    assert [job.job_id for job in page.jobs] == [12000001, 12000002, 12000003]


def test_positive_stage_count_without_finalized_jobs_raises() -> None:
    html = fixture("stage-page-1.html").replace(
        "job-card job-card__wrapper job-card__wrapper--col", "not-a-job-card"
    )

    with pytest.raises(FashionJobsParseError, match="no job cards"):
        parse_page(html, expected_url=STAGE_URL)


def test_missing_third_muted_value_raises() -> None:
    html = fixture("stage-page-1.html").replace(
        "muted-text muted-text--no-bold muted-text--light", "missing-muted-value", 1
    )

    with pytest.raises(FashionJobsParseError, match="required field"):
        parse_page(html, expected_url=STAGE_URL)


def test_excludes_a_valid_non_stage_contract() -> None:
    html = fixture("stage-page-1.html").replace("<span>Stage</span>", "<span>CDD</span>", 1)

    page = parse_page(html, expected_url=STAGE_URL)

    assert [job.job_id for job in page.jobs] == [12000002, 12000003]
    assert page.excluded_contracts == ("CDD",)


def test_a_complete_contract_filter_leak_fails_instead_of_looking_empty() -> None:
    html = fixture("stage-page-1.html").replace("<span>Stage</span>", "<span>CDD</span>")

    with pytest.raises(FashionJobsParseError, match="no Stage job cards"):
        parse_page(html, expected_url=STAGE_URL)


def test_missing_checked_stage_marker_fails_the_page() -> None:
    html = fixture("stage-page-1.html").replace('value="5" checked', 'value="5"', 1)

    with pytest.raises(FashionJobsParseError, match="checked Stage filter"):
        parse_page(html, expected_url=STAGE_URL)


def test_a_malformed_card_fails_instead_of_being_skipped() -> None:
    html = fixture("stage-page-1.html").replace(
        ' data-value="2026-08-21T21:50:27+02:00"', "", 1
    )

    with pytest.raises(FashionJobsParseError, match="publication timestamp"):
        parse_page(html, expected_url=STAGE_URL)


def test_relative_french_text_is_not_used_as_the_timestamp() -> None:
    html = fixture("stage-page-1.html").replace("il y a une heure", "texte local modifié", 1)

    page = parse_page(html, expected_url=STAGE_URL)

    assert page.jobs[0].published_at.isoformat() == "2026-08-21T21:50:27+02:00"


def test_a_wrong_canonical_route_fails() -> None:
    html = fixture("stage-page-1.html").replace(
        "/fr/contrat/Stage,5.html", "/fr/emploi.html", 1
    )

    with pytest.raises(FashionJobsParseError, match="canonical"):
        parse_page(html, expected_url=STAGE_URL)


def test_matching_non_stage_canonical_and_expected_url_fail() -> None:
    non_stage_url = "https://fr.fashionjobs.com/fr/emploi.html"
    html = fixture("stage-page-1.html").replace(
        "/fr/contrat/Stage,5.html", "/fr/emploi.html", 1
    )

    with pytest.raises(FashionJobsParseError, match="Stage route"):
        parse_page(html, expected_url=non_stage_url)


def test_zero_stage_results_are_a_valid_empty_page() -> None:
    page = parse_page(fixture("empty-stage-page.html"), expected_url=STAGE_URL)

    assert page.jobs == ()
    assert page.result_count == 0
    assert page.next_url is None
    assert page.last_page == 1


def test_nonzero_stage_count_without_cards_fails() -> None:
    html = fixture("empty-stage-page.html").replace("Stage (0)", "Stage (1)")

    with pytest.raises(FashionJobsParseError, match="no job cards"):
        parse_page(html, expected_url=STAGE_URL)
