import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from fashionjobs.types import FashionItem, FashionJob, KnownJob

STAGE_LABEL = "Stage"
STAGE_URL = "https://fr.fashionjobs.com/fr/contrat/Stage,5.html"
_FASHIONJOBS_ORIGIN = "fr.fashionjobs.com"
_STAGE_PAGE_URL = re.compile(
    r"^https://fr\.fashionjobs\.com/fr/contrat/Stage,5(?:,([1-9]\d*))?\.html$"
)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class FashionJobsError(RuntimeError):
    pass


class FashionJobsParseError(FashionJobsError):
    pass


class FashionJobsHTTPError(FashionJobsError):
    def __init__(self, status_code: int, *, retryable: bool) -> None:
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(f"FashionJobs request failed: HTTP {status_code}")


@dataclass(frozen=True)
class ParsedPage:
    jobs: tuple[FashionJob, ...]
    result_count: int
    next_url: str | None
    last_page: int
    excluded_contracts: tuple[str, ...] = ()


def page_url(page: int) -> str:
    if page < 1:
        raise ValueError("FashionJobs page must be positive")
    if page == 1:
        return STAGE_URL
    return f"https://fr.fashionjobs.com/fr/contrat/Stage,5,{page}.html"


def parse_page(html: str, *, expected_url: str) -> ParsedPage:
    parser = _FashionJobsPageParser(expected_url)
    parser.feed(html)
    parser.close()
    return parser.result()


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


class FashionJobsSource:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        initial_keys: set[str] | None,
        log: Callable[[str], None],
    ) -> None:
        self._client = client
        self._log = log
        self._known_ids = {int(key) for key in initial_keys or set()}
        self._records: dict[int, FashionJob] = {}
        self._force_full_scan = initial_keys is None

    async def fetch(self) -> list[FashionItem]:
        return await self._fetch_transaction()

    async def _fetch_transaction(self) -> list[FashionItem]:
        candidate_records: dict[int, FashionJob] = {}
        candidate_ids: set[int] = set()
        excluded_contracts: list[str] = []
        visited_urls: set[str] = set()
        frontier = set(self._known_ids)
        discovered_ids: set[int] = set()
        page = 1
        last_page: int | None = None

        while last_page is None or page <= last_page:
            requested_url = page_url(page)
            if requested_url in visited_urls:
                raise FashionJobsParseError("FashionJobs pagination cycle detected")
            visited_urls.add(requested_url)
            parsed = await self._request_page(page)

            if last_page is None:
                last_page = parsed.last_page
            elif parsed.last_page != last_page:
                raise FashionJobsParseError("FashionJobs pagination end changed during traversal")

            self._merge_candidate_jobs(candidate_records, parsed.jobs)
            page_ids = {job.job_id for job in parsed.jobs}
            candidate_ids.update(page_ids)
            excluded_contracts.extend(parsed.excluded_contracts)

            if page < last_page and parsed.next_url != page_url(page + 1):
                raise FashionJobsParseError("FashionJobs next pagination URL did not match")
            if page == last_page and parsed.next_url is not None:
                raise FashionJobsParseError("FashionJobs final page unexpectedly has a next URL")

            has_unseen_ids = bool(page_ids - frontier - discovered_ids)
            discovered_ids.update(page_ids)
            if not self._force_full_scan and not has_unseen_ids:
                break
            page += 1

        self._force_full_scan = False
        self._known_ids.update(candidate_ids)
        self._records.update(candidate_records)
        for contract in excluded_contracts:
            self._log(f"FashionJobs excluded non-Stage contract: {contract}")
        return self._items()

    async def _request_page(self, page: int) -> ParsedPage:
        requested_url = page_url(page)
        response = await self._client.get(requested_url, follow_redirects=False)
        if not 200 <= response.status_code < 300:
            raise FashionJobsHTTPError(
                response.status_code,
                retryable=_is_retryable_status(response.status_code),
            )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise FashionJobsError("FashionJobs response did not have an HTML content type")
        return parse_page(response.text, expected_url=requested_url)

    @staticmethod
    def _merge_candidate_jobs(
        candidate_records: dict[int, FashionJob], jobs: tuple[FashionJob, ...]
    ) -> None:
        for job in jobs:
            existing = candidate_records.get(job.job_id)
            if existing is None:
                candidate_records[job.job_id] = job
                continue
            if _job_core(existing) != _job_core(job):
                raise FashionJobsParseError(
                    f"FashionJobs duplicate job {job.job_id} changed core fields"
                )
            if "/emploi/" in job.url and "/redir/" in existing.url:
                candidate_records[job.job_id] = job

    def _items(self) -> list[FashionItem]:
        return [
            self._records.get(job_id, KnownJob(job_id=job_id))
            for job_id in sorted(self._known_ids)
        ]


def _job_core(job: FashionJob) -> tuple[int, str, str, str, str, datetime]:
    return (
        job.job_id,
        job.title,
        job.company,
        job.contract,
        job.location,
        job.published_at,
    )


@dataclass
class _JobCard:
    title: str | None = None
    url: str | None = None
    company: str | None = None
    muted_values: list[str] = field(default_factory=list)
    published_at: datetime | None = None


class _FashionJobsPageParser(HTMLParser):
    def __init__(self, expected_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._expected_url = expected_url
        self._canonical_url: str | None = None
        self._has_stage_contract = False
        self._stage_heading: tuple[int, list[str]] | None = None
        self._stage_heading_seen = False
        self._next_url: str | None = None
        self._end_url: str | None = None
        self._page_text: list[str] = []
        self._jobs: list[FashionJob] = []
        self._excluded_contracts: list[str] = []
        self._completed_cards = 0
        self._card: _JobCard | None = None
        self._card_depth: int | None = None
        self._capture: tuple[str, int, list[str]] | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID_TAGS:
            self._depth += 1
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())

        if tag == "link" and attributes.get("rel") == "canonical":
            self._canonical_url = attributes.get("href")
        if (
            tag == "input"
            and attributes.get("name") == "contrats[]"
            and attributes.get("value") == "5"
            and "checked" in attributes
        ):
            self._has_stage_contract = True
        if tag == "a" and attributes.get("rel") == "next":
            self._next_url = attributes.get("href")
        if tag == "a" and attributes.get("rel") == "end":
            self._end_url = attributes.get("href")
        if tag == "h1":
            self._stage_heading = (self._depth, [])

        if self._card is None and {"job-card", "job-card__wrapper--col"} <= classes:
            self._card = _JobCard()
            self._card_depth = self._depth
            return
        if self._card is None:
            return

        url = attributes.get("href") or attributes.get("data-lien")
        title = attributes.get("title")
        if url is not None and title is not None:
            self._card.url = url
            self._card.title = _normalize(title)
        elif (
            "extended-link" in classes and url is not None
        ) or {"tw-font-secondary", "tw-uppercase"} <= classes:
            self._start_capture("company")
        elif "muted-text" in classes:
            self._start_capture("muted")
        elif "time-ago" in classes:
            value = attributes.get("data-value")
            if value is not None:
                try:
                    self._card.published_at = datetime.fromisoformat(value)
                except ValueError as error:
                    raise FashionJobsParseError(
                        f"invalid publication timestamp: {value}"
                    ) from error

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._stage_heading is not None and self._stage_heading[0] == self._depth:
            _, parts = self._stage_heading
            self._stage_heading_seen = STAGE_LABEL in _normalize(" ".join(parts))
            self._stage_heading = None

        if self._capture is not None and self._capture[1] == self._depth:
            kind, _, parts = self._capture
            value = _normalize(" ".join(parts))
            if self._card is not None and value:
                if kind == "company":
                    self._card.company = value
                else:
                    self._card.muted_values.append(value)
            self._capture = None

        if self._card is not None and self._card_depth == self._depth:
            job = self._build_job(self._card, self._completed_cards + 1)
            self._completed_cards += 1
            if job.contract != STAGE_LABEL:
                self._excluded_contracts.append(job.contract)
            else:
                self._jobs.append(job)
            self._card = None
            self._card_depth = None
        self._depth -= 1

    def handle_data(self, data: str) -> None:
        value = _normalize(data)
        if not value:
            return
        self._page_text.append(value)
        if self._stage_heading is not None:
            self._stage_heading[1].append(value)
        if self._capture is not None:
            self._capture[2].append(value)

    def result(self) -> ParsedPage:
        if _STAGE_PAGE_URL.fullmatch(self._expected_url) is None:
            raise FashionJobsParseError(
                "FashionJobs requested URL did not match the Stage route"
            )
        if self._canonical_url is None or _STAGE_PAGE_URL.fullmatch(self._canonical_url) is None:
            raise FashionJobsParseError(
                "FashionJobs canonical URL did not match the Stage route"
            )
        if self._canonical_url != self._expected_url:
            raise FashionJobsParseError(
                "FashionJobs canonical URL did not match the requested page"
            )
        if not self._has_stage_contract:
            raise FashionJobsParseError("FashionJobs page did not prove a checked Stage filter")
        if not self._stage_heading_seen:
            raise FashionJobsParseError("FashionJobs page did not expose the Stage result heading")

        count_match = re.search(r"\bStage\s*\(([\d\s]+)\)", " ".join(self._page_text))
        if count_match is None:
            raise FashionJobsParseError("FashionJobs page did not expose a Stage result count")
        result_count = int(count_match.group(1).replace(" ", ""))
        if result_count > 0 and self._completed_cards == 0:
            raise FashionJobsParseError("FashionJobs claimed results but exposed no job cards")
        if result_count > 0 and not self._jobs:
            raise FashionJobsParseError(
                "FashionJobs claimed results but exposed no Stage job cards"
            )

        next_url = self._pagination_url(self._next_url, "next")
        end_url = self._pagination_url(self._end_url, "end")
        end_match = _STAGE_PAGE_URL.fullmatch(end_url) if end_url is not None else None
        last_page = int(end_match.group(1)) if end_match is not None and end_match.group(1) else 1

        return ParsedPage(
            jobs=tuple(self._jobs),
            result_count=result_count,
            next_url=next_url,
            last_page=last_page,
            excluded_contracts=tuple(self._excluded_contracts),
        )

    def _start_capture(self, kind: str) -> None:
        if self._capture is None:
            self._capture = (kind, self._depth, [])

    @staticmethod
    def _build_job(card: _JobCard, position: int) -> FashionJob:
        if not card.title:
            raise FashionJobsParseError(f"FashionJobs card {position} missing title")
        if not card.url:
            raise FashionJobsParseError(f"FashionJobs card {position} missing URL")
        if not card.company:
            raise FashionJobsParseError(f"FashionJobs card {position} missing company")
        if card.published_at is None:
            raise FashionJobsParseError(
                f"FashionJobs card {position} missing publication timestamp"
            )
        if card.published_at.tzinfo is None or card.published_at.utcoffset() is None:
            raise FashionJobsParseError(
                f"FashionJobs card {position} has a timezone-naive publication timestamp"
            )
        if len(card.muted_values) < 3:
            raise FashionJobsParseError(
                f"FashionJobs card {position} missing required field: publication metadata"
            )
        contract = card.muted_values[0]
        if not contract:
            raise FashionJobsParseError(f"FashionJobs card {position} missing contract")
        location = card.muted_values[1]
        if not location:
            raise FashionJobsParseError(f"FashionJobs card {position} missing location")

        parsed_url = urlparse(card.url)
        if parsed_url.scheme != "https" or parsed_url.netloc != _FASHIONJOBS_ORIGIN:
            raise FashionJobsParseError(f"FashionJobs card {position} has an invalid job URL")
        id_match = re.search(r"(?:,|/redir/)(\d+)(?:,|\.html)", card.url)
        if id_match is None:
            raise FashionJobsParseError(f"FashionJobs card {position} has no numeric job ID")
        return FashionJob(
            job_id=int(id_match.group(1)),
            title=card.title,
            company=card.company,
            contract=contract,
            location=location,
            published_at=card.published_at,
            url=card.url,
        )

    @staticmethod
    def _pagination_url(url: str | None, rel: str) -> str | None:
        if url is None:
            return None
        if _STAGE_PAGE_URL.fullmatch(url) is None:
            raise FashionJobsParseError(f"FashionJobs {rel} pagination URL is invalid")
        return url


def _normalize(value: str) -> str:
    return " ".join(value.split())
