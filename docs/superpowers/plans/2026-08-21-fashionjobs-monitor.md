# FashionJobs Internship Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a production-ready third monitor that discovers France-wide FashionJobs internships and sends one Discord notification per newly observed listing.

**Architecture:** A FashionJobs-specific `HTMLParser` reads the fixed France `Stage` result route, and a transactional source traverses only the pagination frontier while retaining every observed ID. A thin app adapter renders one message per job and delegates baseline suppression, delivery settlement, persistence, retry/backoff, liveness, heartbeat, jitter, and shutdown to the unchanged shared runner.

**Tech Stack:** Python 3.13, `httpx`, standard-library `html.parser`, the shared `monitor` workspace package, `pytest`, `mypy --strict`, Ruff, Docker, Terraform, and systemd.

**Spec:** `docs/superpowers/specs/2026-08-21-fashionjobs-monitor-design.md`

## Global Constraints

- Work only on `feat/fashionjobs-monitor`; never commit implementation to `main`.
- The fixed source is `https://fr.fashionjobs.com/fr/contrat/Stage,5.html`.
- The fixed product filter is contract exactly `Stage`, anywhere in France, all roles, and no keywords.
- Tests use committed fixtures and `httpx.MockTransport`; no test contacts FashionJobs or Discord.
- Preserve the shared runner and parity harness unchanged.
- A source or pagination failure cannot become an empty successful read.
- First startup baselines silently; notification-covered keys remain out of state until delivery succeeds.
- Default polling is 600 seconds with the runner's existing 20 percent jitter.
- Default heartbeat time is `07:00` in `America/Denver`; the HTTP health server remains disabled.
- Do not read `infra/terraform.tfvars`, deploy, apply Terraform, run `infra/deploy.sh`, push images, restart services, or touch sibling TypeScript repositories.
- Use conventional commits with titles no longer than 80 characters, brief bodies, and no AI attribution.
- The recorded pre-change baseline is 301 passing tests.

---

## File Structure

New application files:

- `apps/fashionjobs/pyproject.toml` — workspace package metadata and dependencies.
- `apps/fashionjobs/src/fashionjobs/__init__.py` — package marker.
- `apps/fashionjobs/src/fashionjobs/types.py` — immutable job and known-ID types.
- `apps/fashionjobs/src/fashionjobs/site.py` — fixed routes, strict HTML parser, HTTP source, pagination, and observation cache.
- `apps/fashionjobs/src/fashionjobs/alert.py` — Discord-safe formatting for one listing.
- `apps/fashionjobs/src/fashionjobs/monitor.py` — four-method shared-runner adapter.
- `apps/fashionjobs/src/fashionjobs/config.py` — typed operational configuration.
- `apps/fashionjobs/src/fashionjobs/main.py` — process wiring and state-frontier seed.

New test files:

- `tests/fashionjobs/__init__.py` — test package marker.
- `tests/fashionjobs/test_site.py` — parser, HTTP, pagination, and frontier behavior.
- `tests/fashionjobs/test_alert.py` — Discord payload and escaping.
- `tests/fashionjobs/test_monitor.py` — adapter, baseline, restart, retry, order, and count behavior.
- `tests/fashionjobs/test_config.py` — operational defaults and overrides.
- `tests/fashionjobs/fixtures/empty-stage-page.html` — structurally valid zero-result response.

Existing fixtures `stage-page-1.html` and `stage-page-2.html` remain sanitized discovery evidence. Infrastructure and documentation changes stay in their existing files; no generic Docker, systemd, shared-library, or parity file is added.

---

### Task 1: Package, domain types, and representative parsing

**Files:**
- Create: `apps/fashionjobs/pyproject.toml`
- Create: `apps/fashionjobs/src/fashionjobs/__init__.py`
- Create: `apps/fashionjobs/src/fashionjobs/types.py`
- Create: `apps/fashionjobs/src/fashionjobs/site.py`
- Create: `tests/fashionjobs/__init__.py`
- Create: `tests/fashionjobs/test_site.py`
- Modify: `pyproject.toml:3-5`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: `httpx.AsyncClient` later in this file; Task 1 itself uses only the standard library.
- Produces in `fashionjobs.types`: `FashionJob`, `KnownJob`, and `FashionItem`.
- Produces in `fashionjobs.site`: `STAGE_URL`, `STAGE_LABEL`, `page_url()`, `ParsedPage`, `FashionJobsError`, `FashionJobsParseError`, and `parse_page()`.

- [ ] **Step 1: Add failing representative parser tests**

Create literal expectations from the sanitized fixture rather than calculating them from parser helpers:

```python
from datetime import datetime
from pathlib import Path

from fashionjobs.site import STAGE_URL, page_url, parse_page

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
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest tests/fashionjobs/test_site.py::test_stage_route_is_fixed_and_has_no_keyword_or_location_query tests/fashionjobs/test_site.py::test_extracts_ordinary_and_promoted_stage_cards -q
```

Expected: collection fails because `fashionjobs` does not exist.

- [ ] **Step 3: Add the workspace package and immutable types**

Use the existing app metadata shape:

```toml
[project]
name = "fashionjobs"
version = "2.0.0"
description = "FashionJobs France internship monitor"
requires-python = ">=3.13"
dependencies = ["monitor", "httpx>=0.28"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/fashionjobs"]

[tool.uv.sources]
monitor = { workspace = true }
```

Add `apps/fashionjobs` to the root workspace members, run `uv sync`, and define:

```python
from dataclasses import dataclass
from datetime import datetime
from typing import TypeAlias


@dataclass(frozen=True)
class FashionJob:
    job_id: int
    title: str
    company: str
    location: str
    contract: str
    published_at: datetime
    url: str


@dataclass(frozen=True)
class KnownJob:
    job_id: int


FashionItem: TypeAlias = FashionJob | KnownJob
```

- [ ] **Step 4: Implement the narrow page parser**

Define the final public shape before filling in the `HTMLParser` state machine:

```python
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
```

The private parser must collect the canonical link, checked `contrats[]=5` marker,
Stage count, job-card boundaries, title URL from either `href` or `data-lien`, title,
company in either observed markup form, the two semantic muted values (contract and
location), optional localized timestamp display text, exactly one absolute
`time-ago[data-value]`, `rel=next`, and `rel=end`. Decode entities through
`HTMLParser(convert_charrefs=True)`, normalize runs of whitespace, parse timestamps
with `datetime.fromisoformat`, and extract IDs from either `,ID.html` or
`/redir/ID,variant.html`. Only validated job routes may set title and identity;
titled `/fr/recrutement/` links remain company links, and conflicting supported job
links fail the card.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run:

```bash
uv run pytest tests/fashionjobs/test_site.py::test_stage_route_is_fixed_and_has_no_keyword_or_location_query tests/fashionjobs/test_site.py::test_extracts_ordinary_and_promoted_stage_cards -q
uv run mypy --strict apps/fashionjobs tests/fashionjobs/test_site.py
uv run ruff check apps/fashionjobs tests/fashionjobs/test_site.py
```

Expected: 2 tests pass and both static checks exit 0.

- [ ] **Step 6: Commit the representative parser**

```bash
git add pyproject.toml uv.lock apps/fashionjobs tests/fashionjobs
git commit -m "feat: parse FashionJobs Stage listings"
```

---

### Task 2: Strict filter and structural validation

**Files:**
- Create: `tests/fashionjobs/fixtures/empty-stage-page.html`
- Modify: `apps/fashionjobs/src/fashionjobs/site.py`
- Modify: `tests/fashionjobs/test_site.py`

**Interfaces:**
- Consumes: `ParsedPage`, `FashionJobsParseError`, and `parse_page()` from Task 1.
- Produces: fail-closed filter/card/pagination validation and `excluded_contracts` reporting.

- [ ] **Step 1: Add failing strictness tests**

Add cases with literal expected outcomes:

```python
import pytest

from fashionjobs.site import FashionJobsParseError


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
    html = fixture("stage-page-1.html").replace("/fr/contrat/Stage,5.html", "/fr/emploi.html", 1)
    with pytest.raises(FashionJobsParseError, match="canonical"):
        parse_page(html, expected_url=STAGE_URL)
```

Create a small committed zero-result fixture retaining the actual source markers:

```html
<!doctype html>
<html lang="fr">
  <head>
    <link rel="canonical" href="https://fr.fashionjobs.com/fr/contrat/Stage,5.html">
  </head>
  <body>
    <h1>Toutes les offres d'emploi Stage</h1>
    <input type="checkbox" name="contrats[]" value="5" checked>
    <span>Stage (0)</span>
    <ul class="job-list"></ul>
  </body>
</html>
```

Assert it yields zero jobs, count zero, no next URL, and last page 1. Also assert a
nonzero Stage count with no cards raises instead of becoming an empty success.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run pytest tests/fashionjobs/test_site.py -q
```

Expected: the strict validation and empty-result cases fail while Task 1 cases remain green.

- [ ] **Step 3: Complete fail-closed parser validation**

In `result()` require all of the following before constructing `ParsedPage`:

```python
if self.canonical_url != self.expected_url:
    raise FashionJobsParseError("FashionJobs canonical URL did not match the requested page")
if not self.stage_filter_checked:
    raise FashionJobsParseError("FashionJobs page did not prove a checked Stage filter")
if not self.stage_heading_seen:
    raise FashionJobsParseError("FashionJobs page did not expose the Stage result heading")
if self.result_count is None:
    raise FashionJobsParseError("FashionJobs page did not expose a Stage result count")
if self.result_count > 0 and not self.cards:
    raise FashionJobsParseError("FashionJobs claimed results but exposed no job cards")
```

For every completed card, require a numeric ID, nonempty title/company/location,
contract, timezone-aware absolute timestamp, and on-origin HTTPS URL. A valid card
whose normalized contract differs from `Stage` enters `excluded_contracts` and not
`jobs`. Any missing field raises with the card position and field name. Require
pagination links to use `https://fr.fashionjobs.com` and the exact Stage route shape.
After contract filtering, a positive result count with zero Stage jobs also raises;
this prevents a complete server-side filter leak from looking like a legitimate empty
Stage result.

- [ ] **Step 4: Run tests and static checks**

```bash
uv run pytest tests/fashionjobs/test_site.py -q
uv run mypy --strict apps/fashionjobs tests/fashionjobs/test_site.py
uv run ruff check apps/fashionjobs tests/fashionjobs/test_site.py
```

Expected: all FashionJobs site tests pass and both static checks exit 0.

- [ ] **Step 5: Prove the Stage and malformed-card guards are load-bearing**

Temporarily change the contract exclusion condition from `contract != STAGE_LABEL` to
`False`. Run `test_excludes_a_valid_non_stage_contract`; it must fail because ID
12000001 is emitted. Restore the exact condition with `apply_patch`.

Temporarily replace the missing-publication
`raise FashionJobsParseError("FashionJobs card 1 missing publication timestamp")`
with a card skip. Run `test_a_malformed_card_fails_instead_of_being_skipped`; it must fail
because no exception is raised. Restore the raise with `apply_patch`. Record both
failing test names and failure reasons for the PR description.

- [ ] **Step 6: Commit strict source validation**

```bash
git add apps/fashionjobs/src/fashionjobs/site.py tests/fashionjobs
git commit -m "feat: validate FashionJobs result structure"
```

---

### Task 3: HTTP errors and complete transactional pagination

**Files:**
- Modify: `apps/fashionjobs/src/fashionjobs/site.py`
- Modify: `tests/fashionjobs/test_site.py`

**Interfaces:**
- Consumes: Task 2's `parse_page()` and `ParsedPage`.
- Produces: `FashionJobsHTTPError(status_code: int, retryable: bool)` and `FashionJobsSource(client, initial_keys, log)` with `async fetch() -> list[FashionItem]`.

- [ ] **Step 1: Add failing `MockTransport` tests**

Add a response helper that always attaches the request and `content-type: text/html`.
Use a two-page test view by changing the discovery fixture's `rel=end` URL from page
42 to page 2 and removing page 2's `rel=next`; retain literal expected IDs
`12000001`, `12000002`, `12000003`, and `11999999`.

Define `PAGE1_TWO` by replacing page 1's end URL `Stage,5,42.html` with
`Stage,5,2.html`. Define `PAGE2_LAST` by removing its exact page-3 next anchor and
changing its end URL from page 42 to page 2. Then add this complete happy-path test:

```python
async def test_missing_state_fetches_every_declared_page() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        body = PAGE1_TWO if str(request.url) == STAGE_URL else PAGE2_LAST
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=None, log=lambda _message: None)
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2)]
    assert {item.job_id for item in items} == {11999999, 12000001, 12000002, 12000003}
```

Add a duplicate test asserting ID `12000003`, which appears on both fixtures, occurs
once and retains its direct `/emploi/` URL. Add a partial-failure test whose handler
returns page 1 and then HTTP 503; assert `FashionJobsHTTPError`, switch the same handler
to page-2 success, call `fetch()` again, and assert the recorded request sequence is
`[page1, page2, page1, page2]`.

Use one parameterized status test with literal classification:

```python
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
```

Add a transport test whose handler raises `httpx.ConnectError("offline",
request=request)` and assert the exception escapes rather than returning `[]`. Add a
200 `application/json` test expecting `FashionJobsError` containing `HTML`. Finally,
replace page 1's next URL once with page 3 and once with
`https://example.com/Stage,5,2.html`; both variants must raise
`FashionJobsParseError` before requesting a second page.

- [ ] **Step 2: Run only the HTTP/pagination tests and verify RED**

```bash
uv run pytest tests/fashionjobs/test_site.py -q -k "fetches_every or duplicate_ids or failure or non_html or next_page"
```

Expected: failures because `FashionJobsSource` and HTTP classification do not exist.

- [ ] **Step 3: Implement HTTP classification and sequential validation**

Use this public error shape:

```python
class FashionJobsHTTPError(FashionJobsError):
    def __init__(self, status_code: int, *, retryable: bool) -> None:
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(f"FashionJobs request failed: HTTP {status_code}")


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500
```

`_request_page(page)` must request only `page_url(page)`, require status 200–299,
require an HTML content type, and parse using that exact request URL. Do not follow a
redirect and do not implement a tight retry loop. Let `httpx.RequestError` escape as a
failed read with no cache mutation.

Create the source with the final constructor used by all later tasks:

```python
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
        # Task 3 implements complete transactional traversal; Task 4 narrows a
        # seeded read at the first all-known page without changing this interface.
        return await self._fetch_transaction()
```

For a full traversal, pin the first page's declared `last_page`, require every later
page to report the same value, require page N's next link to equal `page_url(N + 1)`
when N is before the end, require no next link at the end, and keep a visited URL set
to reject cycles. Merge duplicates by ID. Core fields must match; if only URLs differ,
prefer the direct `/emploi/` URL over `/redir/`.

- [ ] **Step 4: Make the read transactional**

Accumulate `candidate_jobs`, `candidate_ids`, and requested URLs in local variables.
Do not assign to `_known_ids` or `_records` until the traversal returns successfully:

```python
candidate_records: dict[int, FashionJob] = {}
candidate_ids: set[int] = set()
# fetch and validate every required page into locals
self._known_ids.update(candidate_ids)
self._records.update(candidate_records)
return self._items()
```

Log each excluded non-Stage contract after successful traversal. Do not log a
successful poll until the caller owns it.

- [ ] **Step 5: Run focused and static checks**

```bash
uv run pytest tests/fashionjobs/test_site.py -q
uv run mypy --strict apps/fashionjobs tests/fashionjobs/test_site.py
uv run ruff check apps/fashionjobs tests/fashionjobs/test_site.py
```

Expected: all site tests pass and static checks exit 0.

- [ ] **Step 6: Prove page completion is load-bearing**

Temporarily move `_known_ids.update(candidate_ids)` inside the per-page loop. Run
`test_page_two_failure_discards_the_whole_candidate_read`; it must fail because the
second attempt incorrectly treats page-1 IDs as already observed. Restore the
post-traversal assignment.

Temporarily stop a missing-state traversal after page 1. Run
`test_missing_state_fetches_every_declared_page`; it must fail on the missing page-2
ID and request count. Restore full traversal and record both mutation failures.

- [ ] **Step 7: Commit the paginated source**

```bash
git add apps/fashionjobs/src/fashionjobs/site.py tests/fashionjobs/test_site.py
git commit -m "feat: add FashionJobs paginated source"
```

---

### Task 4: Incremental frontier and monotonic identity retention

**Files:**
- Modify: `apps/fashionjobs/src/fashionjobs/site.py`
- Modify: `tests/fashionjobs/test_site.py`

**Interfaces:**
- Consumes: `FashionJobsSource` from Task 3 and `FashionItem` from Task 1.
- Produces: restart-aware early stopping and a monotonic result containing `KnownJob` placeholders for retained IDs.

- [ ] **Step 1: Add failing frontier tests**

Use separate transports that record exact requested URLs. The first two tests are:

```python
async def test_seeded_frontier_stops_after_an_all_known_page() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=PAGE1_TWO, headers={"content-type": "text/html"})

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
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    seed = {"12000002", "12000003", "11999999"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FashionJobsSource(client, initial_keys=seed, log=lambda _message: None)
        items = await source.fetch()

    assert requested == [STAGE_URL, page_url(2)]
    assert {item.job_id for item in items} == {11999999, 12000001, 12000002, 12000003}
```

For reorder/removal, let the handler return `PAGE1_TWO` on the first call and the
committed zero-result fixture on the second; seed all page-1 IDs, perform two fetches,
and assert both returned ID sets remain `{12000001, 12000002, 12000003}`. For an
explicit empty baseline, instantiate with `initial_keys=set()`, return `PAGE1_TWO` and
`PAGE2_LAST`, and assert both URLs are requested and all four literal IDs are returned.
For full-record retention, fetch page 1 with only ID 12000001 absent from the seed,
then return the zero-result page and assert the retained item for 12000001 is still a
`FashionJob`, not a `KnownJob`.

- [ ] **Step 2: Run the frontier tests and verify RED**

```bash
uv run pytest tests/fashionjobs/test_site.py -q -k "frontier or unseen or reordered or empty_baseline or full_record"
```

Expected: early-stop and placeholder assertions fail against full-pagination-only behavior.

- [ ] **Step 3: Implement frontier traversal and placeholders**

Initialize source state exactly as follows:

```python
self._known_ids = {int(key) for key in initial_keys or set()}
self._records: dict[int, FashionJob] = {}
self._force_full_scan = initial_keys is None
```

At the start of each fetch, snapshot `frontier = set(self._known_ids)`. A missing-state
read follows every page. Otherwise, after each successfully parsed page, continue only
when that page contains at least one ID absent from `frontier`; stop at the first page
containing only frontier IDs or at the declared end. IDs found on earlier pages in the
same read do not make later duplicate appearances “new.”

After successful traversal set `_force_full_scan = False`, merge candidates, and
return deterministic items by numeric ID:

```python
def _items(self) -> list[FashionItem]:
    return [
        self._records.get(job_id, KnownJob(job_id=job_id))
        for job_id in sorted(self._known_ids)
    ]
```

Keep every full `FashionJob` observed during the process so an ID withheld by the
runner after a failed notification remains renderable even if it disappears from the
site before the next poll.

- [ ] **Step 4: Run tests and static checks**

```bash
uv run pytest tests/fashionjobs/test_site.py -q
uv run mypy --strict apps/fashionjobs tests/fashionjobs/test_site.py
uv run ruff check apps/fashionjobs tests/fashionjobs/test_site.py
```

Expected: all site tests and static checks pass.

- [ ] **Step 5: Prove retained placeholders are load-bearing**

Temporarily return only `_records.values()` from `_items()`. Run
`test_reordered_or_removed_cards_do_not_shrink_returned_ids`; it must fail because a
persisted seed with no current card disappears from the key set. Restore the
`KnownJob` fallback and record the failure.

- [ ] **Step 6: Commit frontier retention**

```bash
git add apps/fashionjobs/src/fashionjobs/site.py tests/fashionjobs/test_site.py
git commit -m "feat: retain FashionJobs identities across polls"
```

---

### Task 5: Discord payload and monitor adapter

**Files:**
- Create: `apps/fashionjobs/src/fashionjobs/alert.py`
- Create: `apps/fashionjobs/src/fashionjobs/monitor.py`
- Create: `tests/fashionjobs/test_alert.py`
- Create: `tests/fashionjobs/test_monitor.py`

**Interfaces:**
- Consumes: `FashionJob`, `FashionItem`, `FashionJobsSource`, and shared `Embed`, `Field`, `GREEN`, `HeartbeatExtras`, `Message`, `Monitor`, and `Payload`.
- Produces: `discord_text(value, limit)`, `format_job_alert(job)`, an app-local `FashionJobsReader` protocol, and `FashionJobsMonitor` implementing the four shared methods.

- [ ] **Step 1: Add failing alert tests**

Create a literal `_job()` fixture and assert:

```python
def test_alert_contains_every_reliable_job_field() -> None:
    message = format_job_alert(_job())
    embed = message.payload.embeds[0]
    assert embed.title == "Stage Assistant Produit"
    assert embed.url.endswith(",12000001.html")
    assert [(field.name, field.value) for field in embed.fields or ()] == [
        ("Company", "MAISON EXEMPLE"),
        ("Location", "Paris"),
        ("Contract", "Stage"),
        ("Published", "<t:1787341827:F> · <t:1787341827:R>"),
    ]
    assert message.covers == ("12000001",)
    assert message.payload.allowed_mentions_parse == ()


def test_source_markdown_is_escaped_and_everyone_is_disabled() -> None:
    message = format_job_alert(_job(title="**@everyone**", company="A_B`C"))
    assert message.payload.embeds[0].title == r"\*\*@everyone\*\*"
    assert message.payload.embeds[0].fields is not None
    assert message.payload.embeds[0].fields[0].value == r"A\_B\`C"
    assert message.payload.to_dict()["allowed_mentions"] == {"parse": []}


def test_escaped_title_and_fields_respect_discord_limits_without_dangling_escape() -> None:
    message = format_job_alert(_job(title="*" * 400, company="_" * 1500))
    embed = message.payload.embeds[0]
    assert len(embed.title) <= 256
    assert embed.title.endswith("…")
    assert not embed.title.endswith("\\")
    assert embed.fields is not None
    assert len(embed.fields[0].value) <= 1024
    assert embed.fields[0].value.endswith("…")
    assert not embed.fields[0].value.endswith("\\")
```

The limit test uses repeated `*` characters, asserts title length at most 256, field
value length at most 1024, an ellipsis on truncation, and no terminal backslash.

- [ ] **Step 2: Add failing monitor tests**

Use this typed fake source and return jobs in reverse timestamp order:

```python
class _FakeSource:
    def __init__(self, items: list[FashionItem]) -> None:
        self.items = items

    async def fetch(self) -> list[FashionItem]:
        return list(self.items)


async def test_render_orders_new_jobs_by_timestamp_then_numeric_id() -> None:
    newer = _job(job_id=3, published_at=datetime.fromisoformat("2026-08-21T11:00:00+02:00"))
    same_time_high = _job(job_id=2, published_at=datetime.fromisoformat("2026-08-21T10:00:00+02:00"))
    same_time_low = _job(job_id=1, published_at=datetime.fromisoformat("2026-08-21T10:00:00+02:00"))
    monitor = FashionJobsMonitor(_FakeSource([newer, same_time_high, same_time_low]))
    messages = await monitor.render([newer, same_time_high, same_time_low])
    assert [message.covers for message in messages] == [("1",), ("2",), ("3",)]


def test_key_uses_only_the_numeric_job_id() -> None:
    monitor = FashionJobsMonitor(_FakeSource([]))
    assert monitor.key(_job(job_id=12000001, title="edited")) == "12000001"
    assert monitor.key(KnownJob(job_id=12000001)) == "12000001"


async def test_a_fresh_placeholder_fails_render_instead_of_banking_an_empty_alert() -> None:
    monitor = FashionJobsMonitor(_FakeSource([]))
    with pytest.raises(FashionJobsError, match="12000001.*no job details"):
        await monitor.render([KnownJob(job_id=12000001)])


def test_heartbeat_extras_are_empty() -> None:
    monitor = FashionJobsMonitor(_FakeSource([]))
    assert monitor.heartbeat_extras() == HeartbeatExtras()
```

- [ ] **Step 3: Run the focused tests and verify RED**

```bash
uv run pytest tests/fashionjobs/test_alert.py tests/fashionjobs/test_monitor.py -q
```

Expected: collection fails because alert and monitor modules do not exist.

- [ ] **Step 4: Implement bounded Discord escaping and one-job payloads**

`discord_text()` iterates characters, prefixes `\\` to `\\`, `*`, `_`, `` ` ``, `~`,
and `|`, and stops before the escaped result plus `…` would exceed the supplied
limit. Never slice an already escaped string. Use title limit 256 and field-value
limit 1024.

Build exactly one message per job:

```python
def format_job_alert(job: FashionJob) -> Message:
    unix = int(job.published_at.timestamp())
    embed = Embed(
        title=discord_text(job.title, 256),
        description="New FashionJobs internship",
        color=GREEN,
        url=job.url,
        fields=(
            Field(name="Company", value=discord_text(job.company, 1024)),
            Field(name="Location", value=discord_text(job.location, 1024)),
            Field(name="Contract", value=discord_text(job.contract, 1024)),
            Field(name="Published", value=f"<t:{unix}:F> · <t:{unix}:R>", inline=False),
        ),
        footer_text="FashionJobs.com France",
    )
    return Message(
        payload=Payload(embeds=(embed,), allowed_mentions_parse=()),
        covers=(str(job.job_id),),
    )
```

- [ ] **Step 5: Implement the four-method adapter**

```python
class FashionJobsReader(Protocol):
    async def fetch(self) -> list[FashionItem]: ...


class FashionJobsMonitor:
    def __init__(self, source: FashionJobsReader) -> None:
        self._source = source

    async def fetch(self) -> list[FashionItem]:
        return await self._source.fetch()

    def key(self, item: FashionItem) -> str:
        return str(item.job_id)

    async def render(self, new: list[FashionItem]) -> list[Message]:
        jobs: list[FashionJob] = []
        for item in new:
            if not isinstance(item, FashionJob):
                raise FashionJobsError(f"new FashionJobs ID {item.job_id} has no job details")
            jobs.append(item)
        jobs.sort(key=lambda job: (job.published_at, job.job_id))
        return [format_job_alert(job) for job in jobs]

    def heartbeat_extras(self) -> HeartbeatExtras:
        return HeartbeatExtras()
```

Add a `TYPE_CHECKING` protocol assertion matching the existing apps.

- [ ] **Step 6: Run focused and static checks**

```bash
uv run pytest tests/fashionjobs/test_alert.py tests/fashionjobs/test_monitor.py -q
uv run mypy --strict apps/fashionjobs tests/fashionjobs
uv run ruff check apps/fashionjobs tests/fashionjobs
```

Expected: all new tests and static checks pass.

- [ ] **Step 7: Prove deterministic sorting is load-bearing**

Temporarily remove `jobs.sort(key=lambda job: (job.published_at, job.job_id))`. Run
`test_render_orders_new_jobs_by_timestamp_then_numeric_id`; it must fail with the
reverse `covers` order. Restore the sort and record the mutation result.

- [ ] **Step 8: Commit notifications and adapter**

```bash
git add apps/fashionjobs/src/fashionjobs/alert.py apps/fashionjobs/src/fashionjobs/monitor.py tests/fashionjobs/test_alert.py tests/fashionjobs/test_monitor.py
git commit -m "feat: render FashionJobs Discord alerts"
```

---

### Task 6: Baseline, restart, delivery retry, and tracked count

**Files:**
- Modify: `tests/fashionjobs/test_monitor.py`

**Interfaces:**
- Consumes: `FashionJobsMonitor`, shared `load_runner_config()`, `run_tick()`, and shared heartbeat formatting. Task 7 later replaces the local config helper with the app schema.
- Produces: integration evidence for shared-runner behavior with FashionJobs items; no production API.

- [ ] **Step 1: Add a local runner config helper**

Until Task 7 creates app config, construct `RunnerConfig` through the shared loader:

```python
from monitor.config import RunnerConfig, load_runner_config
from monitor.types import OpsLabels, Payload

LABELS = OpsLabels(
    name="FashionJobs internship monitor",
    tracked_noun="listing identity(ies)",
    death_footer="Liveness alert — check FashionJobs Stage listings manually.",
)


def runner_config() -> RunnerConfig:
    return load_runner_config(
        {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/test"},
        labels=LABELS,
        log_prefix="fashionjobs-monitor",
        default_poll_interval_sec=600,
    )
```

Use no-op async `post_status` and `sleep` functions and collect alert payloads in a
list.

- [ ] **Step 2: Add integration tests**

Add reusable no-op callables and exact first-run/restart checks:

```python
async def no_status(_url: str, _payload: Payload) -> None:
    return None


async def no_sleep(_seconds: float) -> None:
    return None


async def test_first_run_baselines_all_jobs_without_alerting() -> None:
    jobs = [_job(job_id=1), _job(job_id=2)]
    monitor = FashionJobsMonitor(_FakeSource(jobs))
    posted: list[Payload] = []

    async def poster(_url: str, payload: Payload) -> None:
        posted.append(payload)

    keys = await run_tick(
        monitor,
        runner_config(),
        set(),
        is_first_run=True,
        poster=poster,
        post_status=no_status,
        sleep=no_sleep,
        log=lambda _message: None,
    )
    assert keys == {"1", "2"}
    assert posted == []


async def test_restart_with_persisted_ids_does_not_duplicate_alerts() -> None:
    monitor = FashionJobsMonitor(_FakeSource([_job(job_id=1), _job(job_id=2)]))
    posted: list[Payload] = []

    async def poster(_url: str, payload: Payload) -> None:
        posted.append(payload)

    keys = await run_tick(
        monitor,
        runner_config(),
        {"1", "2"},
        is_first_run=False,
        poster=poster,
        post_status=no_status,
        sleep=no_sleep,
        log=lambda _message: None,
    )
    assert keys == {"1", "2"}
    assert posted == []
```

Exercise notification withholding and retry with the same job object:

```python
async def test_failed_notification_is_withheld_then_retried_successfully() -> None:
    monitor = FashionJobsMonitor(_FakeSource([_job(job_id=7)]))

    async def failing_poster(_url: str, _payload: Payload) -> None:
        raise RuntimeError("Discord down")

    first_keys = await run_tick(
        monitor,
        runner_config(),
        set(),
        is_first_run=False,
        poster=failing_poster,
        post_status=no_status,
        sleep=no_sleep,
        log=lambda _message: None,
    )
    assert first_keys == set()

    posted: list[Payload] = []

    async def successful_poster(_url: str, payload: Payload) -> None:
        posted.append(payload)

    second_keys = await run_tick(
        monitor,
        runner_config(),
        first_keys,
        is_first_run=False,
        poster=successful_poster,
        post_status=no_status,
        sleep=no_sleep,
        log=lambda _message: None,
    )
    assert second_keys == {"7"}
    assert len(posted) == 1
```

For multiple new jobs, pass IDs 3, 2, 1 with timestamps matching Task 5's order test,
collect each payload's embed title, and assert the titles arrive in ID order 1, 2, 3.
For the count test, build `state = replace(init_health(1000), items_tracked=4)`, call
`format_heartbeat(LABELS, state, 4600)`, and assert its description contains
`tracking 4 listing identity(ies)`.

- [ ] **Step 3: Run the integration tests**

```bash
uv run pytest tests/fashionjobs/test_monitor.py -q
```

Expected: all integration tests pass using the unchanged shared runner.

- [ ] **Step 4: Verify existing runner tests still pass**

```bash
uv run pytest tests/lib/test_runner.py tests/fashionjobs/test_monitor.py -q
```

Expected: existing settlement tests and FashionJobs integration tests all pass.

- [ ] **Step 5: Commit the integration evidence**

```bash
git add tests/fashionjobs/test_monitor.py
git commit -m "test: prove FashionJobs delivery invariants"
```

---

### Task 7: Typed configuration and process entry point

**Files:**
- Create: `apps/fashionjobs/src/fashionjobs/config.py`
- Create: `apps/fashionjobs/src/fashionjobs/main.py`
- Create: `tests/fashionjobs/test_config.py`
- Modify: `tests/fashionjobs/test_monitor.py`

**Interfaces:**
- Consumes: shared `load_runner_config`, `load_state`, `post`, `run_forever`, and Task 5's monitor.
- Produces: `FashionJobsConfig`, `LABELS`, `LOG_PREFIX`, `load_config()`, and executable `python -m fashionjobs.main`.

- [ ] **Step 1: Add failing config tests**

```python
from fashionjobs.config import LABELS, LOG_PREFIX, load_config

WEBHOOK = "https://discord.com/api/webhooks/1/test"


def test_defaults_are_respectful_and_health_server_is_off() -> None:
    cfg = load_config({"DISCORD_WEBHOOK_URL": WEBHOOK})
    assert cfg.runner.poll_interval_sec == 600
    assert cfg.runner.poll_jitter_pct == 20
    assert cfg.runner.heartbeat_at is None
    assert not hasattr(cfg.runner, "health_port")
    assert LOG_PREFIX == "fashionjobs-monitor"
    assert LABELS.tracked_noun == "listing identity(ies)"


def test_operational_runner_values_can_be_overridden() -> None:
    cfg = load_config(
        {
            "DISCORD_WEBHOOK_URL": WEBHOOK,
            "POLL_INTERVAL_SEC": "900",
            "HEARTBEAT_AT": "07:00",
        }
    )
    assert cfg.runner.poll_interval_sec == 900
    assert cfg.runner.heartbeat_at == (7, 0)


def test_product_filters_are_not_configuration_fields() -> None:
    cfg = load_config({"DISCORD_WEBHOOK_URL": WEBHOOK, "KEYWORDS": "styliste"})
    assert not hasattr(cfg, "keywords")
    assert not hasattr(cfg, "contract")
    assert not hasattr(cfg, "location")
```

- [ ] **Step 2: Run config tests and verify RED**

```bash
uv run pytest tests/fashionjobs/test_config.py -q
```

Expected: collection fails because `fashionjobs.config` does not exist.

- [ ] **Step 3: Implement the app schema**

```python
LOG_PREFIX = "fashionjobs-monitor"

LABELS = OpsLabels(
    name="FashionJobs internship monitor",
    tracked_noun="listing identity(ies)",
    death_footer="Liveness alert — check FashionJobs Stage listings manually.",
)


@dataclass(frozen=True)
class FashionJobsConfig:
    runner: RunnerConfig


def load_config(env: Env) -> FashionJobsConfig:
    return FashionJobsConfig(
        runner=load_runner_config(
            env,
            labels=LABELS,
            log_prefix=LOG_PREFIX,
            default_poll_interval_sec=600,
        )
    )
```

Replace Task 6's local labels/config helper with imports from this module.
The shared loader leaves `HEARTBEAT_AT` unset for a bare local environment; Task 8's
Terraform default supplies `07:00` for the production service, matching the other apps.

- [ ] **Step 4: Wire the process without changing shared code**

`main.py` must:

1. Load config and log `filter=Stage country=France keywords=none` plus the poll interval.
2. Install shared shutdown handlers.
3. Call `load_state(cfg.runner.state_path)` once and pass `loaded.keys` as the source's `initial_keys`; do not treat an empty set as missing.
4. Create one `httpx.AsyncClient` with a 20-second timeout, `follow_redirects=False`, `Accept: text/html`, and `User-Agent: fashionjobs-monitor/2.0`.
5. Use the same client for FashionJobs and Discord.
6. Call `run_forever()` with the same fatal-error handling pattern as both existing apps.

The runner still loads the same state independently and remains its sole writer.

- [ ] **Step 5: Run focused tests and import smoke check**

```bash
uv run pytest tests/fashionjobs/test_config.py tests/fashionjobs/test_monitor.py -q
uv run python -c "import fashionjobs.main"
uv run mypy --strict apps/fashionjobs tests/fashionjobs
uv run ruff check apps/fashionjobs tests/fashionjobs
```

Expected: tests pass, the import prints nothing and exits 0, and static checks exit 0.

- [ ] **Step 6: Commit configuration and wiring**

```bash
git add apps/fashionjobs/src/fashionjobs/config.py apps/fashionjobs/src/fashionjobs/main.py tests/fashionjobs
git commit -m "feat: wire FashionJobs monitor process"
```

---

### Task 8: Container and Terraform integration

**Files:**
- Modify: `infra/apps.auto.tfvars:10-22`
- Modify: `infra/variables.tf:106-166`
- Modify: `infra/main.tf:55-91`
- Modify: `infra/terraform.tfvars.example:17-28`

**Interfaces:**
- Consumes: the existing generic root `Dockerfile`, `monitor.service.tftpl`, and `startup.sh.tftpl` unchanged.
- Produces: a `fashionjobs` app map entry, per-app environment, validated Terraform variables, persistent `/data/state.json`, and a 128 MiB memory cap.

- [ ] **Step 1: Add FashionJobs Terraform variables**

Add required/sensitive `fashionjobs_discord_webhook_url`, optional/sensitive
`fashionjobs_status_webhook_url`, positive numeric
`fashionjobs_poll_interval_sec` defaulting to 600, and
`fashionjobs_heartbeat_at` defaulting to `07:00` with the existing empty-or-HH:MM
regex validation. Keep product filters absent.

- [ ] **Step 2: Add the app and environment maps**

Add this committed app record:

```hcl
fashionjobs = {
  image     = "fashionjobs-monitor"
  image_tag = "v2.1.0"
  memory    = "128m"
}
```

Add `local.app_env.fashionjobs` with:

```hcl
fashionjobs = merge(
  {
    DISCORD_WEBHOOK_URL    = var.fashionjobs_discord_webhook_url
    STATE_PATH             = "/data/state.json"
    POLL_INTERVAL_SEC      = tostring(var.fashionjobs_poll_interval_sec)
    HEARTBEAT_INTERVAL_SEC = tostring(var.heartbeat_interval_sec)
    STALL_ALERT_SEC        = tostring(var.stall_alert_sec)
  },
  var.fashionjobs_status_webhook_url != "" ? { STATUS_WEBHOOK_URL = var.fashionjobs_status_webhook_url } : {},
  var.fashionjobs_heartbeat_at != "" ? { HEARTBEAT_AT = var.fashionjobs_heartbeat_at } : {},
)
```

The generic startup template derives the service name `fashionjobs-monitor`, volume
`/var/lib/fashionjobs-data`, environment file, and persistent bind mount.

- [ ] **Step 3: Document only placeholder credentials in the example tfvars**

Add:

```hcl
# --- fashionjobs-monitor (France-wide Stage listings) ---
fashionjobs_discord_webhook_url = "https://discord.com/api/webhooks/xxxx/yyyy"
# fashionjobs_status_webhook_url = "https://discord.com/api/webhooks/xxxx/zzzz"
# fashionjobs_poll_interval_sec  = 600
# fashionjobs_heartbeat_at       = "07:00" # America/Denver; "" = drift with process start
```

Do not open, format, copy, or inspect `infra/terraform.tfvars`.

- [ ] **Step 4: Format only explicitly safe Terraform files**

```bash
terraform fmt -check infra/versions.tf
terraform fmt -check infra/variables.tf
terraform fmt -check infra/main.tf
terraform fmt -check infra/outputs.tf
terraform fmt -check infra/apps.auto.tfvars
```

If a check fails, run `terraform fmt` against that exact listed file and rerun its
check. Never point `terraform fmt` at the `infra` directory.

- [ ] **Step 5: Validate Terraform from a secret-free temporary copy**

Create and validate an explicitly scoped temporary directory, then copy only the safe
configuration files and templates:

```bash
FASHIONJOBS_TF_CHECK_DIR="$(mktemp -d /tmp/fashionjobs-tf.XXXXXX)"
case "$FASHIONJOBS_TF_CHECK_DIR" in
  /tmp/fashionjobs-tf.*) ;;
  *) exit 1 ;;
esac
cp infra/versions.tf infra/variables.tf infra/main.tf infra/outputs.tf infra/apps.auto.tfvars "$FASHIONJOBS_TF_CHECK_DIR/"
cp -R infra/templates "$FASHIONJOBS_TF_CHECK_DIR/templates"
terraform -chdir="$FASHIONJOBS_TF_CHECK_DIR" init -backend=false
terraform -chdir="$FASHIONJOBS_TF_CHECK_DIR" validate
rm -rf -- "$FASHIONJOBS_TF_CHECK_DIR"
```

The task-specific variable must contain the exact `mktemp -d` result. Remove that
exact temporary directory after validation. Do not run `terraform plan`; its required
webhook values are intentionally unavailable without the prohibited secrets file.

- [ ] **Step 6: Validate the generic container target and memory cap**

```bash
docker build --platform linux/amd64 --build-arg APP=fashionjobs -t fashionjobs-monitor:local .
docker run --rm --memory=128m fashionjobs-monitor:local python -c "import fashionjobs.main; print('fashionjobs import ok')"
```

Expected: the build exits 0 and the capped container prints `fashionjobs import ok`.
Do not push the image.

- [ ] **Step 7: Commit infrastructure integration**

```bash
git add infra/apps.auto.tfvars infra/variables.tf infra/main.tf infra/terraform.tfvars.example
git commit -m "feat: configure FashionJobs service"
```

---

### Task 9: Architecture, invariants, and operations documentation

**Files:**
- Modify: `README.md:7-11,24-28,79-83`
- Modify: `apps/README.md:11-24,46-62`
- Modify: `infra/README.md:112-176`
- Modify: `docs/architecture.md:8-35,73-96,149-161,185-199`
- Modify: `docs/invariants.md:221-259`

**Interfaces:**
- Consumes: completed app behavior, actual test names, and validated infrastructure from Tasks 1–8.
- Produces: operator-facing behavior, log commands, architectural ownership, and load-bearing FashionJobs guards.

- [ ] **Step 1: Update repository and app documentation**

Change the README's FashionJobs row from `planned` to the exact fixed source and state
that alerts are France-wide Stage listings with no keyword filtering. Add
`./infra/logs.sh fashionjobs` beside the two existing examples. Run test collection and
replace the README's stale inline test-count comment with the collected total:

```bash
uv run pytest --collect-only -q
```

Extend `apps/README.md` with FashionJobs as the example of app-owned source parsing,
fixed product filtering, and retained identity placeholders; keep the four-method
shared contract unchanged.

- [ ] **Step 2: Document operational commands**

Add FashionJobs to the three-app Docker build loop and log examples. Document:

```bash
./logs.sh fashionjobs --freshness=6h
systemctl status fashionjobs-monitor
journalctl -u fashionjobs-monitor -n 50
```

State that the operator must supply the FashionJobs Discord webhook and build/push the
`v2.1.0` image before a later authorized deployment. Do not claim this PR deploys it.

- [ ] **Step 3: Update architecture and invariants**

Add the third app to the architecture tree and describe:

- fixed Stage/France HTML source and strict per-page proof;
- full missing-state baseline followed by the unseen-ID frontier;
- monotonic placeholders and the “listing identities seen” heartbeat count;
- one message per listing with `(published_at, job_id)` ordering;
- ordinary/promoted duplicate reconciliation;
- shared runner ownership remaining unchanged.

Append FashionJobs-specific invariant sections naming the exact tests from Tasks 2–6:
exact Stage filtering, no malformed-card skip, transactional required pagination,
monotonic retained IDs, promoted-card coverage without crawling `/redir/`, and
deterministic alert order. Keep the parity section explicitly limited to Melanzana and
Jeffco.

- [ ] **Step 4: Check documentation and focused tests**

```bash
git diff --check
uv run pytest tests/fashionjobs -q
uv run ruff format --check .
```

Expected: no whitespace errors, all FashionJobs tests pass, and formatting check exits 0.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md apps/README.md infra/README.md docs/architecture.md docs/invariants.md
git commit -m "docs: operate FashionJobs monitor"
```

---

### Task 10: Final verification and reviewable PR

**Files:**
- Modify only files implicated by a failing gate; do not weaken tests or invariants.
- Create no committed credential, build-output, or Terraform state files.

**Interfaces:**
- Consumes: the complete branch and every mutation record from Tasks 2–5.
- Produces: gate evidence, a clean branch, and a reviewable GitHub PR; no deployment.

- [ ] **Step 1: Run the required gates and record each exit status**

Run in this exact order:

```bash
uv sync
uv run pytest -q
uv run mypy --strict lib apps tests tools
uv run ruff check .
uv run ruff format --check .
./tools/parity-diff.sh
```

Capture the final pytest count. If parity cannot run, record whether Node, the
Melanzana sibling, or the Jeffco sibling is absent; do not modify the script or add
FashionJobs to it.

- [ ] **Step 2: Repeat static infrastructure validation without secrets**

Repeat the explicit-file Terraform formatting checks and secret-free temporary-copy
`terraform init -backend=false` plus `terraform validate` from Task 8. Rebuild the
FashionJobs Docker target and repeat the 128 MiB import smoke test. Record every exit
status. Do not run a Terraform plan, apply, deploy script, image push, or service
restart.

- [ ] **Step 3: Review the complete branch**

Check:

```bash
git status --short --branch
git diff --check main...HEAD
git log --oneline main..HEAD
```

Review `git diff main...HEAD` for credentials, webhook values other than documented
placeholders, accidental shared-runner/parity changes, live-test calls, and commit
titles over 80 characters. Fix only genuine findings, rerun affected focused tests,
and create a small conventional commit for each concern.

- [ ] **Step 4: Prepare the PR evidence**

The PR body must include these concrete sections:

- user behavior: Stage only, anywhere France, all roles, no keywords, next-poll alerts;
- source discovery: canonical HTML route, structured filter ID 5, 1,266 active results,
  42 pages, 19 same-day observations, and estimated 15–25 per day;
- 600-second polling plus 20 percent jitter and robots rationale;
- numeric FJOB identity, pagination frontier, monotonic state, first-run baseline, and
  notification withholding;
- committed fixtures and exact mutation failures from Tasks 2–5;
- workspace, image, Terraform, systemd, state path, webhook, and docs changes;
- baseline 301 plus final pytest count and their arithmetic difference;
- every gate command with its recorded exit status and any exact skip reason;
- 14 top-level discovery requests plus one isolated browser load/static resources and
  no Discord discovery traffic;
- 128 MiB FashionJobs cap, total declared app caps of 512 MiB, and `e2-micro` impact;
- remaining setup: provide webhook, build/push `v2.1.0`, and perform a separately
  authorized deployment;
- explicit statement that nothing was deployed, applied, pushed as an image, or
  restarted.

- [ ] **Step 5: Push the feature branch and create the PR**

Source `~/.zshrc` without printing its values, verify GitHub authentication, then run:

```bash
git push -u origin feat/fashionjobs-monitor
gh pr create --base main --head feat/fashionjobs-monitor --title "feat: add FashionJobs internship monitor" --body-file /tmp/fashionjobs-pr.md
```

Create `/tmp/fashionjobs-pr.md` from the exact recorded evidence in Step 4 using
`apply_patch`; do not include AI attribution or credentials. Return the PR URL and a
concise gate summary. Do not merge it.
