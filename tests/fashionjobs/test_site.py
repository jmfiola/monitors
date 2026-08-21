from datetime import datetime
from pathlib import Path

import httpx
import pytest
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


def test_positive_count_equal_to_cards_allows_a_single_page() -> None:
    html = fixture("stage-page-1.html").replace("Stage (1266)", "Stage (3)")
    single_page = html[: html.index('<nav class="pagination">')] + "</body></html>"

    page = parse_page(single_page, expected_url=STAGE_URL)

    assert len(page.jobs) == 3
    assert page.result_count == 3
    assert page.next_url is None
    assert page.last_page == 1


def test_positive_count_less_than_stage_cards_fails() -> None:
    html = fixture("stage-page-1.html").replace("Stage (1266)", "Stage (2)")

    with pytest.raises(FashionJobsParseError, match=r"fewer results.*Stage job cards"):
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


async def test_missing_state_fetches_every_declared_page() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        body = PAGE1_TWO if str(request.url) == STAGE_URL else PAGE2_LAST
        return html_response(request, body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2)]
    assert {item.job_id for item in items} == {11999999, 12000001, 12000002, 12000003}


async def test_seeded_frontier_stops_after_an_all_known_page() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return html_response(request, PAGE1_TWO)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(
            client,
            initial_keys={"12000001", "12000002", "12000003"},
            log=lambda _message: None,
        )
        items = await source.fetch()

    assert requested == [STAGE_URL]
    assert {item.job_id for item in items} == {12000001, 12000002, 12000003}


async def test_an_unseen_page_one_id_continues_until_an_all_known_page() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        body = PAGE1_TWO if str(request.url) == STAGE_URL else PAGE2_LAST
        return html_response(request, body)

    seed = {"12000002", "12000003", "11999999"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=seed, log=lambda _message: None)
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2)]
    assert {item.job_id for item in items} == {11999999, 12000001, 12000002, 12000003}


async def test_same_read_duplicate_does_not_extend_frontier_traversal() -> None:
    requested: list[str] = []
    page_one = PAGE1_TWO.replace(
        f'<a rel="end" href="{page_url(2)}">42</a>',
        f'<a rel="end" href="{page_url(3)}">42</a>',
    )
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
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2)]
    assert [item.job_id for item in items] == [12000001, 12000002, 12000003]


async def test_reordered_or_removed_cards_do_not_shrink_returned_ids() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = PAGE1_TWO if calls == 1 else fixture("empty-stage-page.html")
        return html_response(request, body)

    seed = {"11999999", "12000001", "12000002", "12000003"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=seed, log=lambda _message: None)
        first = await source.fetch()
        second = await source.fetch()

    expected = [11999999, 12000001, 12000002, 12000003]
    assert [item.job_id for item in first] == expected
    assert [item.job_id for item in second] == expected


async def test_empty_baseline_walks_while_pages_introduce_ids() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        body = PAGE1_TWO if str(request.url) == STAGE_URL else PAGE2_LAST
        return html_response(request, body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=set(), log=lambda _message: None)
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2)]
    assert {item.job_id for item in items} == {11999999, 12000001, 12000002, 12000003}


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
    page2_with_redirect_duplicate = PAGE2_LAST.replace(
        "https://fr.fashionjobs.com/emploi/atelier-exemple/Stage-assistant-communication,12000003.html",
        "https://fr.fashionjobs.com/redir/12000003,1.html",
        1,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = PAGE1_TWO if str(request.url) == STAGE_URL else page2_with_redirect_duplicate
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
        with pytest.raises(FashionJobsError, match="HTML"):
            await source.fetch()


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
