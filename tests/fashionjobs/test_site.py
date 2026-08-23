from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fashionjobs.alert import format_job_alert
from fashionjobs.site import (
    STAGE_URL,
    FashionJobsError,
    FashionJobsHTTPError,
    FashionJobsParseError,
    FashionJobsSource,
    page_url,
    parse_page,
)
from fashionjobs.types import FashionJob

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


PAGE1_TWO = fixture("stage-page-1.html").replace("Stage,5,42.html", "Stage,5,2.html")
PAGE2_LAST = (
    fixture("stage-page-2.html")
    .replace(
        '      <a rel="next" href="https://fr.fashionjobs.com/fr/contrat/Stage,5,3.html">Suivant</a>\n',
        "",
    )
    .replace("Stage,5,42.html", "Stage,5,2.html")
)


def html_response(request: httpx.Request, body: str) -> httpx.Response:
    return httpx.Response(
        200,
        text=body,
        headers={"content-type": "text/html"},
        request=request,
    )


def with_first_card_appearances(html: str, appearances: int) -> str:
    list_start = html.index(">", html.index('<ul class="job-list">')) + 1
    card_start = html.index("<li ", list_start)
    card_end = html.index("</li>", card_start) + len("</li>")
    list_end = html.index("</ul>", card_end)
    first_card = html[card_start:card_end]
    return html[:list_start] + (first_card * appearances) + html[list_end:]


def with_end_page(html: str, page: int) -> str:
    return html.replace("Stage,5,42.html", f"Stage,5,{page}.html").replace(
        ">42</a>", f">{page}</a>"
    )


def three_page_bodies() -> dict[str, str]:
    page_one = with_end_page(fixture("stage-page-1.html"), 3)
    page_two = with_end_page(with_first_card_appearances(fixture("stage-page-2.html"), 1), 3)
    page_three = (
        page_two.replace(page_url(2), page_url(3), 1)
        .replace(f'<a rel="next" href="{page_url(3)}">Suivant</a>', "", 1)
        .replace("12000003", "11999998")
    )
    return {STAGE_URL: page_one, page_url(2): page_two, page_url(3): page_three}


def page_with_promoted_end(page: int) -> str:
    return (
        fixture("stage-page-2.html")
        .replace(
            f'<link rel="canonical" href="{page_url(2)}">',
            f'<link rel="canonical" href="{page_url(page)}">',
        )
        .replace(
            f'<a rel="next" href="{page_url(3)}">',
            f'<a rel="next" href="{page_url(page + 1)}">',
        )
    )


async def assert_pagination_failure_discards_transaction(
    bodies: dict[str, str],
    *,
    error: str,
) -> None:
    requested: list[str] = []
    failure = True

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        body = bodies[str(request.url)] if failure else fixture("empty-stage-page.html")
        return html_response(request, body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        with pytest.raises(FashionJobsParseError, match=error):
            await source.fetch()
        assert source._known_ids == set()
        assert source._records == {}
        assert source._force_full_scan
        assert source._last_full_scan_at is None
        failure = False
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2), STAGE_URL]
    assert items == []


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


def test_blank_location_slot_is_retained_and_formats_without_empty_alert_fields() -> None:
    html = fixture("stage-page-36-blank-location.html")
    page = parse_page(html, expected_url=page_url(36), expected_final_page=36)

    assert [(job.job_id, job.location) for job in page.jobs] == [(11999996, "")]
    fields = format_job_alert(page.jobs[0]).payload.embeds[0].fields or ()
    assert [(field.name, field.value) for field in fields] == [
        ("Company", "MAISON EXEMPLE"),
        ("Contract", "Stage"),
        ("Published", "<t:1787297400:F> · <t:1787297400:R>"),
    ]
    assert all(field.name and field.value for field in fields)

    location_slot = (
        '              <div class="muted-text muted-text--no-bold muted-text--primary">\n'
        "                <span>   </span>\n"
        "              </div>\n"
    )
    assert location_slot in html
    missing_location_slot = html.replace(location_slot, "", 1)

    with pytest.raises(FashionJobsParseError, match="exactly two metadata fields"):
        parse_page(missing_location_slot, expected_url=page_url(36), expected_final_page=36)


def test_titled_company_link_does_not_overwrite_job_url() -> None:
    html = fixture("stage-page-1.html").replace(
        'data-lien="https://fr.fashionjobs.com/fr/recrutement/maison-exemple.html"',
        'data-lien="https://fr.fashionjobs.com/fr/recrutement/maison-exemple.html" '
        'title="MAISON EXEMPLE"',
        1,
    )

    page = parse_page(html, expected_url=STAGE_URL)

    assert page.jobs[0].title == "Stage Assistant Produit"
    assert page.jobs[0].url.endswith("/Stage-assistant-produit,12000001.html")


def test_conflicting_supported_job_links_fail() -> None:
    second_link = (
        '<a href="https://fr.fashionjobs.com/emploi/autre/Stage-autre,12009999.html" '
        'title="Stage Autre"></a>'
    )
    html = fixture("stage-page-1.html").replace(
        '</a>\n            <div class="tw-font-secondary tw-uppercase">',
        f'</a>{second_link}\n            <div class="tw-font-secondary tw-uppercase">',
        1,
    )

    with pytest.raises(FashionJobsParseError, match="conflicting job links"):
        parse_page(html, expected_url=STAGE_URL)


@pytest.mark.parametrize(
    "invalid_url",
    [
        "https://fr.fashionjobs.com/not-a-job,12000001.html",
        "https://fr.fashionjobs.com/emploi/,12000001.html",
        "https://fr.fashionjobs.com/emploi/maison/titre,0.html",
        "https://fr.fashionjobs.com/emploi/maison/titre,12000001.html?source=test",
        "https://fr.fashionjobs.com/emploi/maison/titre,12000001.html#fragment",
        "https://fr.fashionjobs.com/redir/0,1.html",
        "https://fr.fashionjobs.com/redir/12000001,0.html",
        "https://fr.fashionjobs.com/redir/12000001,1.html?source=test",
        "http://fr.fashionjobs.com/emploi/maison/titre,12000001.html",
        "https://fr.fashionjobs.com:443/emploi/maison/titre,12000001.html",
        "https://example.com/emploi/maison/titre,12000001.html",
    ],
)
def test_rejects_unsupported_job_url_shapes(invalid_url: str) -> None:
    html = fixture("stage-page-1.html").replace(
        "https://fr.fashionjobs.com/emploi/maison-exemple/Stage-assistant-produit,12000001.html",
        invalid_url,
        1,
    )

    with pytest.raises(FashionJobsParseError, match="invalid job URL"):
        parse_page(html, expected_url=STAGE_URL)


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


def test_truncated_document_fails_after_a_complete_card() -> None:
    html = fixture("stage-page-1.html")
    truncated = html[: html.index("</li>") + len("</li>")]

    with pytest.raises(FashionJobsParseError, match="complete HTML document"):
        parse_page(truncated, expected_url=STAGE_URL)


@pytest.mark.parametrize(
    ("closing_fragment", "replacement"),
    [
        ("        </div>\n      </li>", "      </li>"),
        ("    </ul>", ""),
    ],
)
def test_missing_interior_closing_tag_fails(closing_fragment: str, replacement: str) -> None:
    html = fixture("stage-page-1.html").replace(closing_fragment, replacement, 1)

    with pytest.raises(FashionJobsParseError, match="unfinished HTML structure"):
        parse_page(html, expected_url=STAGE_URL)


def test_extra_closing_tag_fails_with_negative_depth() -> None:
    html = fixture("stage-page-1.html") + "</section>"

    with pytest.raises(FashionJobsParseError, match="unfinished HTML structure"):
        parse_page(html, expected_url=STAGE_URL)


@pytest.mark.parametrize(
    "suffix",
    [
        "</section><h1>",
        '</section><div class="job-card job-card__wrapper--col">',
        (
            '</section></section><div class="job-card job-card__wrapper--col">'
            '<div class="muted-text">'
        ),
    ],
)
def test_balanced_depth_with_unfinished_parser_state_fails(suffix: str) -> None:
    html = fixture("stage-page-1.html") + suffix

    with pytest.raises(FashionJobsParseError, match="unfinished parser state"):
        parse_page(html, expected_url=STAGE_URL)


def test_positive_stage_count_without_finalized_jobs_raises() -> None:
    html = fixture("stage-page-1.html").replace(
        "job-card job-card__wrapper job-card__wrapper--col", "not-a-job-card"
    )

    with pytest.raises(FashionJobsParseError, match="no job cards"):
        parse_page(html, expected_url=STAGE_URL)


def test_positive_count_with_insufficient_cards_requires_end_pagination() -> None:
    html = fixture("stage-page-1.html")
    without_pagination = html[: html.index('<nav class="pagination">')] + "</body></html>"

    with pytest.raises(FashionJobsParseError, match="end pagination"):
        parse_page(without_pagination, expected_url=STAGE_URL)


def test_pagination_free_positive_page_requires_exact_learned_final_page() -> None:
    final_page = fixture("stage-page-42-final.html")

    with pytest.raises(FashionJobsParseError, match="end pagination"):
        parse_page(final_page, expected_url=page_url(42))
    parsed = parse_page(
        final_page,
        expected_url=page_url(42),
        expected_final_page=42,
    )

    assert parsed.last_page == 42
    assert parsed.next_url is None
    assert [job.job_id for job in parsed.jobs] == [11999998]


def test_pagination_free_non_page_one_requires_a_learned_final_page() -> None:
    self_consistent_page = fixture("stage-page-42-final.html").replace("Stage (1242)", "Stage (1)")

    with pytest.raises(FashionJobsParseError, match="end pagination"):
        parse_page(self_consistent_page, expected_url=page_url(42))

    parsed = parse_page(
        self_consistent_page,
        expected_url=page_url(42),
        expected_final_page=42,
    )

    assert parsed.last_page == 42
    assert [job.job_id for job in parsed.jobs] == [11999998]


def test_pagination_free_intermediate_page_fails_with_later_final_page_context() -> None:
    intermediate_page = fixture("stage-page-42-final.html").replace(page_url(42), page_url(41))

    with pytest.raises(FashionJobsParseError, match="end pagination"):
        parse_page(
            intermediate_page,
            expected_url=page_url(41),
            expected_final_page=42,
        )


def test_page_one_with_more_results_requires_a_later_end_page() -> None:
    html = (
        fixture("stage-page-1.html")
        .replace(f'<a rel="next" href="{page_url(2)}">Suivant</a>', "", 1)
        .replace(
            f'<a rel="end" href="{page_url(42)}">42</a>',
            f'<a rel="end" href="{STAGE_URL}">1</a>',
            1,
        )
    )

    with pytest.raises(FashionJobsParseError, match=r"page 1.*later end page"):
        parse_page(html, expected_url=STAGE_URL)


def test_page_one_with_every_result_rejects_later_pagination() -> None:
    html = fixture("stage-page-1.html").replace("Stage (1266)", "Stage (3)")

    with pytest.raises(FashionJobsParseError, match=r"page 1.*all declared results"):
        parse_page(html, expected_url=STAGE_URL)


def test_declared_end_at_page_limit_is_valid() -> None:
    page = parse_page(with_end_page(fixture("stage-page-1.html"), 100), expected_url=STAGE_URL)

    assert page.last_page == 100


def test_declared_end_above_page_limit_fails() -> None:
    html = with_end_page(fixture("stage-page-1.html"), 101)

    with pytest.raises(FashionJobsParseError, match="page safety limit"):
        parse_page(html, expected_url=STAGE_URL)


def test_positive_count_equal_to_cards_allows_a_single_page() -> None:
    html = fixture("stage-page-1.html").replace("Stage (1266)", "Stage (3)")
    single_page = html[: html.index('<nav class="pagination">')] + "</body></html>"

    page = parse_page(single_page, expected_url=STAGE_URL)

    assert len(page.jobs) == 3
    assert page.result_count == 3
    assert page.next_url is None
    assert page.last_page == 1


def test_positive_count_less_than_unique_stage_ids_fails() -> None:
    html = fixture("stage-page-1.html").replace("Stage (1266)", "Stage (2)")

    with pytest.raises(FashionJobsParseError, match=r"fewer results.*unique Stage job IDs"):
        parse_page(html, expected_url=STAGE_URL)


def test_duplicate_appearances_do_not_satisfy_a_larger_result_count() -> None:
    html = with_first_card_appearances(fixture("stage-page-1.html"), 3).replace(
        "Stage (1266)", "Stage (3)"
    )
    without_pagination = html[: html.index('<nav class="pagination">')] + "</body></html>"

    with pytest.raises(FashionJobsParseError, match="end pagination"):
        parse_page(without_pagination, expected_url=STAGE_URL)


def test_duplicate_appearances_of_the_only_result_are_valid() -> None:
    html = with_first_card_appearances(fixture("stage-page-1.html"), 2).replace(
        "Stage (1266)", "Stage (1)"
    )
    single_result = html[: html.index('<nav class="pagination">')] + "</body></html>"

    page = parse_page(single_result, expected_url=STAGE_URL)

    assert [job.job_id for job in page.jobs] == [12000001, 12000001]
    assert page.result_count == 1
    assert page.next_url is None
    assert page.last_page == 1


def test_empty_timestamp_display_text_is_valid() -> None:
    html = fixture("stage-page-1.html").replace("il y a une heure", "", 1)

    page = parse_page(html, expected_url=STAGE_URL)

    assert page.jobs[0].published_at == datetime.fromisoformat("2026-08-21T21:50:27+02:00")


def test_duplicate_timestamp_fields_fail() -> None:
    timestamp = (
        '<span class="time-ago" data-value="2026-08-21T21:50:27+02:00">\n'
        "                  il y a une heure\n"
        "                </span>"
    )
    html = fixture("stage-page-1.html").replace(timestamp, timestamp + timestamp, 1)

    with pytest.raises(FashionJobsParseError, match="exactly one publication timestamp"):
        parse_page(html, expected_url=STAGE_URL)


def test_timestamp_wrapper_sibling_metadata_fails() -> None:
    html = fixture("stage-page-1.html").replace(
        "il y a une heure\n                </span>",
        "il y a une heure\n                </span><span>Unexpected</span>",
        1,
    )

    with pytest.raises(FashionJobsParseError, match="exactly two metadata fields"):
        parse_page(html, expected_url=STAGE_URL)


def test_empty_extra_muted_metadata_field_fails() -> None:
    empty_field = '<div class="muted-text muted-text--no-bold muted-text--primary"></div>'
    html = fixture("stage-page-1.html").replace(
        '<div class="muted-text muted-text--no-bold muted-text--light">',
        empty_field + '<div class="muted-text muted-text--no-bold muted-text--light">',
        1,
    )

    with pytest.raises(FashionJobsParseError, match="exactly two metadata fields"):
        parse_page(html, expected_url=STAGE_URL)


def test_void_timestamp_element_fails() -> None:
    timestamp = (
        '<span class="time-ago" data-value="2026-08-21T21:50:27+02:00">\n'
        "                  il y a une heure\n"
        "                </span>"
    )
    void_timestamp = (
        '<input class="time-ago" data-value="2026-08-21T21:50:27+02:00"><span>Unexpected</span>'
    )
    html = fixture("stage-page-1.html").replace(timestamp, void_timestamp, 1)

    with pytest.raises(FashionJobsParseError, match="void publication timestamp"):
        parse_page(html, expected_url=STAGE_URL)


def test_nested_muted_metadata_field_fails() -> None:
    html = fixture("stage-page-1.html").replace(
        "<span>Stage</span>",
        '<div class="muted-text muted-text--no-bold muted-text--primary"></div><span>Stage</span>',
        1,
    )

    with pytest.raises(FashionJobsParseError, match="nested metadata field"):
        parse_page(html, expected_url=STAGE_URL)


def test_extra_muted_metadata_field_fails() -> None:
    extra_field = (
        '<div class="muted-text muted-text--no-bold muted-text--primary">'
        "<span>Unexpected</span></div>"
    )
    html = fixture("stage-page-1.html").replace(
        '<div class="muted-text muted-text--no-bold muted-text--light">',
        extra_field + '<div class="muted-text muted-text--no-bold muted-text--light">',
        1,
    )

    with pytest.raises(FashionJobsParseError, match="exactly two metadata fields"):
        parse_page(html, expected_url=STAGE_URL)


def test_unknown_contract_label_fails() -> None:
    html = fixture("stage-page-1.html").replace(
        "<span>Stage</span>", "<span>Stage premium</span>", 1
    )

    with pytest.raises(FashionJobsParseError, match="unknown contract"):
        parse_page(html, expected_url=STAGE_URL)


@pytest.mark.parametrize("contract", ["CDI", "CDD", "Alternance", "Intérim", "Free-lance"])
def test_excludes_a_recognized_non_stage_contract(contract: str) -> None:
    html = fixture("stage-page-1.html").replace("<span>Stage</span>", f"<span>{contract}</span>", 1)

    page = parse_page(html, expected_url=STAGE_URL)

    assert [job.job_id for job in page.jobs] == [12000002, 12000003]
    assert page.excluded_contracts == (contract,)


def test_a_complete_contract_filter_leak_fails_instead_of_looking_empty() -> None:
    html = fixture("stage-page-1.html").replace("<span>Stage</span>", "<span>CDD</span>")

    with pytest.raises(FashionJobsParseError, match="no Stage job cards"):
        parse_page(html, expected_url=STAGE_URL)


def test_missing_checked_stage_marker_fails_the_page() -> None:
    html = fixture("stage-page-1.html").replace('value="5" checked', 'value="5"', 1)

    with pytest.raises(FashionJobsParseError, match="checked Stage filter"):
        parse_page(html, expected_url=STAGE_URL)


def test_a_malformed_card_fails_instead_of_being_skipped() -> None:
    html = fixture("stage-page-1.html").replace(' data-value="2026-08-21T21:50:27+02:00"', "", 1)

    with pytest.raises(FashionJobsParseError, match="publication timestamp"):
        parse_page(html, expected_url=STAGE_URL)


def test_relative_french_text_is_not_used_as_the_timestamp() -> None:
    html = fixture("stage-page-1.html").replace("il y a une heure", "texte local modifié", 1)

    page = parse_page(html, expected_url=STAGE_URL)

    assert page.jobs[0].published_at.isoformat() == "2026-08-21T21:50:27+02:00"


def test_a_wrong_canonical_route_fails() -> None:
    html = fixture("stage-page-1.html").replace("/fr/contrat/Stage,5.html", "/fr/emploi.html", 1)

    with pytest.raises(FashionJobsParseError, match="canonical"):
        parse_page(html, expected_url=STAGE_URL)


def test_matching_non_stage_canonical_and_expected_url_fail() -> None:
    non_stage_url = "https://fr.fashionjobs.com/fr/emploi.html"
    html = fixture("stage-page-1.html").replace("/fr/contrat/Stage,5.html", "/fr/emploi.html", 1)

    with pytest.raises(FashionJobsParseError, match="Stage route"):
        parse_page(html, expected_url=non_stage_url)


def test_zero_stage_results_are_a_valid_empty_page() -> None:
    page = parse_page(fixture("empty-stage-page.html"), expected_url=STAGE_URL)

    assert page.jobs == ()
    assert page.result_count == 0
    assert page.next_url is None
    assert page.last_page == 1


def test_zero_stage_count_with_cards_fails() -> None:
    html = fixture("stage-page-1.html").replace("Stage (1266)", "Stage (0)")
    without_pagination = html[: html.index('<nav class="pagination">')] + "</body></html>"

    with pytest.raises(FashionJobsParseError, match=r"zero results.*job cards"):
        parse_page(without_pagination, expected_url=STAGE_URL)


def test_zero_stage_count_with_pagination_fails() -> None:
    pagination = (
        f'<a rel="next" href="{page_url(2)}">Suivant</a><a rel="end" href="{page_url(2)}">2</a>'
    )
    html = fixture("empty-stage-page.html").replace("</body>", f"{pagination}</body>")

    with pytest.raises(FashionJobsParseError, match=r"zero results.*pagination"):
        parse_page(html, expected_url=STAGE_URL)


def test_nonzero_stage_count_without_cards_fails() -> None:
    html = fixture("empty-stage-page.html").replace("Stage (0)", "Stage (1)")

    with pytest.raises(FashionJobsParseError, match="no job cards"):
        parse_page(html, expected_url=STAGE_URL)


@pytest.mark.parametrize("initial_keys", [None, set()], ids=["missing", "empty"])
async def test_missing_and_empty_state_cross_duplicate_only_pages(
    initial_keys: set[str] | None,
) -> None:
    requested: list[str] = []
    bodies = three_page_bodies()

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return html_response(request, bodies[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(
            client,
            initial_keys=initial_keys,
            log=lambda _message: None,
            clock=lambda: 0.0,
        )
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2), page_url(3)]
    assert {item.job_id for item in items} == {11999998, 12000001, 12000002, 12000003}


async def test_seeded_startup_scans_to_end_then_frontier_fast_stops() -> None:
    requested: list[str] = []
    bodies = {
        STAGE_URL: PAGE1_TWO,
        page_url(2): with_first_card_appearances(PAGE2_LAST, 1),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return html_response(request, bodies[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(
            client,
            initial_keys={"12000001", "12000002", "12000003"},
            log=lambda _message: None,
        )
        await source.fetch()
        startup_requested = requested.copy()
        requested.clear()
        items = await source.fetch()

    assert startup_requested == [STAGE_URL, page_url(2)]
    assert requested == [STAGE_URL]
    assert {item.job_id for item in items} == {12000001, 12000002, 12000003}


async def test_failed_seeded_startup_scan_retries_the_full_walk() -> None:
    requested: list[str] = []
    bodies = three_page_bodies()
    fail_page_two = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_page_two
        requested.append(str(request.url))
        if str(request.url) == page_url(2) and fail_page_two:
            fail_page_two = False
            return httpx.Response(503, request=request)
        return html_response(request, bodies[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(
            client,
            initial_keys={"12000001", "12000002", "12000003"},
            log=lambda _message: None,
        )
        with pytest.raises(FashionJobsHTTPError):
            await source.fetch()
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2), STAGE_URL, page_url(2), page_url(3)]
    assert {item.job_id for item in items} == {11999998, 12000001, 12000002, 12000003}


async def test_seeded_frontier_still_stops_before_daily_full_scan_is_due() -> None:
    requested: list[str] = []
    now = 0.0
    bodies = {
        STAGE_URL: PAGE1_TWO,
        page_url(2): with_first_card_appearances(PAGE2_LAST, 1),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return html_response(request, bodies[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(
            client,
            initial_keys={"12000001", "12000002", "12000003"},
            log=lambda _message: None,
            clock=lambda: now,
        )
        await source.fetch()
        assert requested == [STAGE_URL, page_url(2)]
        requested.clear()
        now = 86_399.0
        await source.fetch()

    assert requested == [STAGE_URL]


async def test_due_daily_scan_crosses_duplicates_to_find_a_later_new_id() -> None:
    requested: list[str] = []
    bodies = three_page_bodies()
    now = 0.0

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return html_response(request, bodies[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(
            client,
            initial_keys={"12000001", "12000002", "12000003"},
            log=lambda _message: None,
            clock=lambda: now,
        )
        await source.fetch()
        assert requested == [STAGE_URL, page_url(2), page_url(3)]
        requested.clear()
        now = 86_400.0
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2), page_url(3)]
    assert {item.job_id for item in items} == {11999998, 12000001, 12000002, 12000003}


async def test_failed_due_daily_scan_remains_due() -> None:
    requested: list[str] = []
    bodies = three_page_bodies()
    now = 0.0
    fail_page_two = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_page_two
        requested.append(str(request.url))
        if str(request.url) == page_url(2) and fail_page_two:
            fail_page_two = False
            return httpx.Response(503, request=request)
        return html_response(request, bodies[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(
            client,
            initial_keys={"12000001", "12000002", "12000003"},
            log=lambda _message: None,
            clock=lambda: now,
        )
        await source.fetch()
        assert requested == [STAGE_URL, page_url(2), page_url(3)]
        requested.clear()
        now = 86_400.0
        fail_page_two = True
        with pytest.raises(FashionJobsHTTPError):
            await source.fetch()
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2), STAGE_URL, page_url(2), page_url(3)]
    assert {item.job_id for item in items} == {11999998, 12000001, 12000002, 12000003}


async def test_successful_due_daily_scan_resets_the_deadline() -> None:
    requested: list[str] = []
    bodies = three_page_bodies()
    now = 0.0

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return html_response(request, bodies[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(
            client,
            initial_keys={"12000001", "12000002", "12000003"},
            log=lambda _message: None,
            clock=lambda: now,
        )
        await source.fetch()
        assert requested == [STAGE_URL, page_url(2), page_url(3)]
        requested.clear()
        now = 86_400.0
        await source.fetch()
        requested.clear()
        now = 172_799.0
        await source.fetch()

    assert requested == [STAGE_URL]


async def test_an_unseen_page_one_id_continues_until_an_all_known_page() -> None:
    requested: list[str] = []
    steady_state = False
    page_one_with_new_id = PAGE1_TWO.replace("12000001", "12000004")

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == STAGE_URL:
            body = page_one_with_new_id if steady_state else PAGE1_TWO
        else:
            body = PAGE2_LAST
        return html_response(request, body)

    seed = {"12000002", "12000003", "11999999"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=seed, log=lambda _message: None)
        await source.fetch()
        steady_state = True
        requested.clear()
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2)]
    assert {item.job_id for item in items} == {
        11999999,
        12000001,
        12000002,
        12000003,
        12000004,
    }


async def test_frontier_read_crosses_the_highest_promoted_end_before_stopping() -> None:
    requested: list[str] = []
    steady_state = False
    startup_bodies = {
        STAGE_URL: PAGE1_TWO,
        page_url(2): PAGE2_LAST,
    }
    promoted_bodies = {
        STAGE_URL: PAGE1_TWO.replace("12000001", "12000004"),
        page_url(2): with_end_page(page_with_promoted_end(2), 3),
        page_url(3): with_end_page(page_with_promoted_end(3), 4),
        page_url(4): fixture("stage-page-42-final.html").replace(page_url(42), page_url(4)),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        bodies = promoted_bodies if steady_state else startup_bodies
        return html_response(request, bodies[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        await source.fetch()
        requested.clear()
        steady_state = True
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2), page_url(3), page_url(4)]
    assert {item.job_id for item in items} == {
        11999998,
        11999999,
        12000001,
        12000002,
        12000003,
        12000004,
    }


async def test_same_read_duplicate_does_not_extend_frontier_traversal() -> None:
    requested: list[str] = []
    startup_bodies = three_page_bodies()
    steady_state = False
    page_one = PAGE1_TWO.replace(
        f'<a rel="end" href="{page_url(2)}">42</a>',
        f'<a rel="end" href="{page_url(3)}">42</a>',
    ).replace("12000003", "12000004")
    page_two_duplicate = (
        page_one.replace(STAGE_URL, page_url(2), 1)
        .replace(
            f'<a rel="next" href="{page_url(2)}">Suivant</a>',
            f'<a rel="next" href="{page_url(3)}">Suivant</a>',
        )
        .replace("job-card job-card__wrapper job-card__wrapper--col", "not-a-job-card", 2)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if not steady_state:
            return html_response(request, startup_bodies[str(request.url)])
        if str(request.url) == STAGE_URL:
            return html_response(request, page_one)
        if str(request.url) == page_url(2):
            return html_response(request, page_two_duplicate)
        pytest.fail("same-read duplicate requested page three")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(
            client,
            initial_keys={"12000001", "12000002"},
            log=lambda _message: None,
        )
        await source.fetch()
        steady_state = True
        requested.clear()
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2)]
    assert [item.job_id for item in items] == [
        11999998,
        12000001,
        12000002,
        12000003,
        12000004,
    ]


async def test_reordered_or_removed_cards_do_not_shrink_returned_ids() -> None:
    empty = False

    def handler(request: httpx.Request) -> httpx.Response:
        if empty:
            body = fixture("empty-stage-page.html")
        else:
            body = PAGE1_TWO if str(request.url) == STAGE_URL else PAGE2_LAST
        return html_response(request, body)

    seed = {"11999999", "12000001", "12000002", "12000003"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=seed, log=lambda _message: None)
        first = await source.fetch()
        empty = True
        second = await source.fetch()

    expected = [11999999, 12000001, 12000002, 12000003]
    assert [item.job_id for item in first] == expected
    assert [item.job_id for item in second] == expected


async def test_full_record_is_retained_when_it_disappears_from_the_site() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            body = PAGE1_TWO
        elif calls == 2:
            body = PAGE2_LAST
        else:
            body = fixture("empty-stage-page.html")
        return html_response(request, body)

    seed = {"12000002", "12000003", "11999999"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=seed, log=lambda _message: None)
        await source.fetch()
        items = await source.fetch()

    retained = next(item for item in items if item.job_id == 12000001)
    assert isinstance(retained, FashionJob)


async def test_duplicate_ids_keep_the_direct_emploi_url() -> None:
    page1_with_redirect_duplicate = PAGE1_TWO.replace(
        "https://fr.fashionjobs.com/emploi/atelier-exemple/Stage-assistant-communication,12000003.html",
        "https://fr.fashionjobs.com/redir/12000003,1.html",
        1,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = page1_with_redirect_duplicate if str(request.url) == STAGE_URL else PAGE2_LAST
        return html_response(request, body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        items = await source.fetch()

    duplicate_jobs = [item for item in items if item.job_id == 12000003]
    assert len(duplicate_jobs) == 1
    assert isinstance(duplicate_jobs[0], FashionJob)
    assert duplicate_jobs[0].url == (
        "https://fr.fashionjobs.com/emploi/atelier-exemple/"
        "Stage-assistant-communication,12000003.html"
    )


async def test_page_two_failure_discards_the_whole_candidate_read() -> None:
    requested: list[str] = []
    fail_page_two = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_page_two
        requested.append(str(request.url))
        if str(request.url) == STAGE_URL:
            body = PAGE1_TWO if fail_page_two else fixture("empty-stage-page.html")
            return html_response(request, body)
        if fail_page_two:
            fail_page_two = False
            return httpx.Response(503, request=request)
        return html_response(request, PAGE2_LAST)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        with pytest.raises(FashionJobsHTTPError) as caught:
            await source.fetch()
        items = await source.fetch()

    assert caught.value.status_code == 503
    assert requested == [STAGE_URL, page_url(2), STAGE_URL]
    assert items == []


async def test_promoted_end_fetches_the_expanded_pagination_final_page() -> None:
    requested: list[str] = []
    bodies = {
        STAGE_URL: with_end_page(fixture("stage-page-1.html"), 41),
        **{page_url(page): page_with_promoted_end(page) for page in range(2, 42)},
        page_url(42): fixture("stage-page-42-final.html"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return html_response(request, bodies[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        items = await source.fetch()

    assert requested == [page_url(page) for page in range(1, 43)]
    assert [item.job_id for item in items] == [11999998, 11999999, 12000001, 12000002, 12000003]


async def test_backward_pagination_end_failure_discards_candidate_state() -> None:
    requested: list[str] = []
    fail_with_backward_end = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_with_backward_end
        requested.append(str(request.url))
        if str(request.url) == STAGE_URL:
            body = PAGE1_TWO if fail_with_backward_end else fixture("empty-stage-page.html")
        else:
            body = PAGE2_LAST.replace(
                f'<a rel="end" href="{page_url(2)}">',
                f'<a rel="end" href="{STAGE_URL}">',
            )
            fail_with_backward_end = False
        return html_response(request, body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        with pytest.raises(FashionJobsParseError, match="end changed"):
            await source.fetch()
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2), STAGE_URL]
    assert items == []


async def test_learned_final_page_with_multitoken_next_url_fails_before_an_extra_request() -> None:
    requested: list[str] = []
    final_page_with_next = (
        fixture("stage-page-42-final.html")
        .replace(
            page_url(42),
            page_url(2),
        )
        .replace("</body>", f'<a rel="NeXt nofollow" href="{page_url(3)}">Suivant</a></body>')
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        body = PAGE1_TWO if str(request.url) == STAGE_URL else final_page_with_next
        return html_response(request, body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        with pytest.raises(FashionJobsParseError, match="final page unexpectedly has a next URL"):
            await source.fetch()

    assert requested == [STAGE_URL, page_url(2)]


async def test_learned_final_page_with_next_without_href_discards_transaction() -> None:
    final_page_without_next_href = (
        fixture("stage-page-42-final.html")
        .replace(
            page_url(42),
            page_url(2),
        )
        .replace("</body>", '<a rel="next">Suivant</a></body>')
    )

    await assert_pagination_failure_discards_transaction(
        {
            STAGE_URL: PAGE1_TWO,
            page_url(2): final_page_without_next_href,
        },
        error="next pagination URL is missing",
    )


async def test_duplicate_next_pagination_declarations_discard_transaction() -> None:
    duplicate_next_page = with_end_page(fixture("stage-page-2.html"), 3).replace(
        f'<a rel="next" href="{page_url(3)}">Suivant</a>',
        (
            '<a rel="next" href="https://example.com/Stage,5,3.html">Mauvais</a>'
            f'<a rel="next" href="{page_url(3)}">Suivant</a>'
        ),
    )

    await assert_pagination_failure_discards_transaction(
        {
            STAGE_URL: PAGE1_TWO,
            page_url(2): duplicate_next_page,
            page_url(3): fixture("stage-page-42-final.html").replace(page_url(42), page_url(3)),
        },
        error="duplicate next pagination",
    )


async def test_duplicate_end_pagination_declarations_discard_transaction() -> None:
    duplicate_end_page = PAGE2_LAST.replace(
        f'<a rel="end" href="{page_url(2)}">42</a>',
        (f'<a rel="end" href="{STAGE_URL}">1</a><a rel="end" href="{page_url(2)}">42</a>'),
    )

    await assert_pagination_failure_discards_transaction(
        {
            STAGE_URL: PAGE1_TWO,
            page_url(2): duplicate_end_page,
        },
        error="duplicate end pagination",
    )


def with_duplicated_anchor(html: str, *, original: str, duplicate: str) -> str:
    assert html.count(original) == 1
    duplicated_html = html.replace(original, duplicate * 2)
    assert duplicated_html.count(duplicate) == 2
    return duplicated_html


def test_identical_repeated_pagination_declarations_are_accepted() -> None:
    repeated_next = f'<a rel="next" href="{page_url(2)}">Suivant</a>'
    repeated_end = f'<a rel="end" href="{page_url(42)}">42</a>'
    html = with_duplicated_anchor(
        with_duplicated_anchor(
            fixture("stage-page-1.html"),
            original=repeated_next,
            duplicate=repeated_next,
        ),
        original=repeated_end,
        duplicate=repeated_end,
    )

    page = parse_page(html, expected_url=STAGE_URL)

    assert page.next_url == page_url(2)
    assert page.last_page == 42


async def test_identical_repeated_pagination_declarations_traverse_source() -> None:
    repeated_next = f'<a rel="next" href="{page_url(2)}">Suivant</a>'
    repeated_end = f'<a rel="end" href="{page_url(2)}">2</a>'
    page_one = with_duplicated_anchor(
        with_duplicated_anchor(
            fixture("stage-page-1.html"),
            original=repeated_next,
            duplicate=repeated_next,
        ),
        original=f'<a rel="end" href="{page_url(42)}">42</a>',
        duplicate=repeated_end,
    )
    final_page = fixture("stage-page-42-final.html").replace(page_url(42), page_url(2))
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        body = page_one if str(request.url) == STAGE_URL else final_page
        return html_response(request, body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2)]
    assert {item.job_id for item in items} == {11999998, 12000001, 12000002, 12000003}
    assert source._known_ids == {11999998, 12000001, 12000002, 12000003}
    assert not source._force_full_scan


async def test_zero_result_learned_final_page_discards_transaction() -> None:
    empty_final_page = fixture("empty-stage-page.html").replace(STAGE_URL, page_url(2))

    await assert_pagination_failure_discards_transaction(
        {
            STAGE_URL: PAGE1_TWO,
            page_url(2): empty_final_page,
        },
        error="end changed",
    )


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(429, True), (503, True), (403, False), (404, False)],
)
async def test_http_status_classification(status_code: int, retryable: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        with pytest.raises(FashionJobsHTTPError) as caught:
            await source.fetch()

    assert caught.value.status_code == status_code
    assert caught.value.retryable is retryable
    assert f"retryable={str(retryable).lower()}" in str(caught.value)


async def test_transport_failure_escapes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        with pytest.raises(httpx.ConnectError, match="offline"):
            await source.fetch()


async def test_non_html_response_raises_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="{}",
            headers={"content-type": "application/json"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        with pytest.raises(FashionJobsError, match="HTML content type") as caught:
            await source.fetch()

    assert type(caught.value) is FashionJobsError


async def test_page_limit_fails_before_a_second_request() -> None:
    requested: list[str] = []
    oversized = with_end_page(fixture("stage-page-1.html"), 101)

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return html_response(request, oversized)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        with pytest.raises(FashionJobsParseError, match="page safety limit"):
            await source.fetch()

    assert requested == [STAGE_URL]


@pytest.mark.parametrize(
    "invalid_next_url",
    [page_url(3), "https://example.com/Stage,5,2.html"],
)
async def test_invalid_next_page_raises_before_a_second_request(invalid_next_url: str) -> None:
    requested: list[str] = []
    page_with_invalid_next = PAGE1_TWO.replace(page_url(2), invalid_next_url, 1)

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return html_response(request, page_with_invalid_next)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        with pytest.raises(FashionJobsParseError):
            await source.fetch()

    assert requested == [STAGE_URL]
