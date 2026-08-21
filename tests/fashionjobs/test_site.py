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
