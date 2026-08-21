import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser

from fashionjobs.types import FashionJob

STAGE_LABEL = "Stage"
STAGE_URL = "https://fr.fashionjobs.com/fr/contrat/Stage,5.html"


class FashionJobsError(RuntimeError):
    pass


class FashionJobsParseError(FashionJobsError):
    pass


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
        self._next_url: str | None = None
        self._end_url: str | None = None
        self._page_text: list[str] = []
        self._jobs: list[FashionJob] = []
        self._card: _JobCard | None = None
        self._card_depth: int | None = None
        self._capture: tuple[str, int, list[str]] | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
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

    def handle_endtag(self, tag: str) -> None:
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
            self._jobs.append(self._build_job(self._card))
            self._card = None
            self._card_depth = None
        self._depth -= 1

    def handle_data(self, data: str) -> None:
        value = _normalize(data)
        if not value:
            return
        self._page_text.append(value)
        if self._capture is not None:
            self._capture[2].append(value)

    def result(self) -> ParsedPage:
        if self._canonical_url != self._expected_url:
            raise FashionJobsParseError("canonical URL does not match the requested page")
        if not self._has_stage_contract:
            raise FashionJobsParseError("Stage contract marker is missing")

        count_match = re.search(r"\bStage\s*\(([\d\s]+)\)", " ".join(self._page_text))
        if count_match is None:
            raise FashionJobsParseError("Stage result count is missing")
        if self._end_url is None:
            raise FashionJobsParseError("last page link is missing")
        last_page_match = re.search(r",(\d+)\.html$", self._end_url)
        if last_page_match is None:
            raise FashionJobsParseError("last page link is invalid")

        return ParsedPage(
            jobs=tuple(self._jobs),
            result_count=int(count_match.group(1).replace(" ", "")),
            next_url=self._next_url,
            last_page=int(last_page_match.group(1)),
        )

    def _start_capture(self, kind: str) -> None:
        if self._capture is None:
            self._capture = (kind, self._depth, [])

    @staticmethod
    def _build_job(card: _JobCard) -> FashionJob:
        if (
            card.title is None
            or card.url is None
            or card.company is None
            or len(card.muted_values) < 2
            or card.published_at is None
        ):
            raise FashionJobsParseError("job card is missing a required field")
        id_match = re.search(r"(?:,|/redir/)(\d+)(?:,|\.html)", card.url)
        if id_match is None:
            raise FashionJobsParseError(f"job URL has no identifier: {card.url}")
        return FashionJob(
            job_id=int(id_match.group(1)),
            title=card.title,
            company=card.company,
            contract=card.muted_values[0],
            location=card.muted_values[1],
            published_at=card.published_at,
            url=card.url,
        )


def _normalize(value: str) -> str:
    return " ".join(value.split())
