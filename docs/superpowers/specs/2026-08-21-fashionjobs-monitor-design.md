# FashionJobs Internship Monitor Design

Date: 2026-08-21
Status: approved in chat for written-spec review

## Purpose and fixed scope

The third monitor will notify Kaitlin through Discord about newly observed
FashionJobs internships. Her final clarification was: “Just internships but no
keywords.” That maps to the following fixed product rules:

- Contract type is exactly FashionJobs `Stage`.
- Location is anywhere in France.
- Every fashion role and category is included.
- No keyword, title, company, category, region, department, or city filter is
  applied.
- Each new listing produces one Discord message on the next successful poll.
- There is no digest.

These filters are not environment configuration. Making them configurable would
make it possible to deploy a monitor that silently differs from Kaitlin's request.
Only operational settings such as the poll interval and webhooks are configurable.

## Source discovery

### Selected source

The canonical France-wide Stage result page is:

`https://fr.fashionjobs.com/fr/contrat/Stage,5.html`

It is server-rendered HTML and returns HTTP 200 to a normal `httpx` client with a
transparent user agent. It does not require authentication, cookies, JavaScript, or
a browser. Pagination uses path segments:

- page 1: `/fr/contrat/Stage,5.html`
- page 2: `/fr/contrat/Stage,5,2.html`
- page N: `/fr/contrat/Stage,5,N.html`

The page identifies the selected structured filter with a checked
`contrats[]=5` control labelled `Stage`, a Stage-specific heading, and a canonical
URL on the same route. The `fr.fashionjobs.com` France edition supplies the country
scope. Leaving every geographic selector unselected means nationwide France rather
than Paris or a particular region, department, or city.

The monitor will validate those filter markers on every fetched page. The source URL
is a code constant, not an environment variable. It will also require each emitted
card's contract text to equal `Stage`; an otherwise valid non-Stage card is excluded
and logged as a source-filter leak.

### Why the HTML source

The list page's JSON-LD is malformed and omits promoted cards. Fetching valid JSON-LD
from every detail page would add one request per listing without removing the need to
parse and paginate the result list. No usable public API or RSS feed was found.
Browser automation adds substantial memory and failure surface without unlocking a
better source.

The considered approaches were therefore:

1. **Dedicated server-rendered HTML parser (selected).** One public result-page
   request in the normal steady state, complete coverage of ordinary and promoted
   cards, and no new runtime dependency beyond `httpx`.
2. **List HTML plus detail-page JSON-LD.** More normalized metadata, but 15–25 extra
   daily requests and a larger partial-failure surface for fields already present in
   the cards.
3. **Headless browser.** It can render the same page, but adds a browser to the
   `e2-micro` for no source-quality benefit.

The parser will use Python's `html.parser.HTMLParser` with FashionJobs-specific state
and strict structural checks. It will not introduce a general scraping framework.

### Observed inventory, ordering, and pagination

Discovery on 2026-08-21 observed 1,266 active Stage listings across 42 pages. Normal
pages contain 30 organic results. Page 1 contained 47 cards because promoted and
urgent cards are inserted in addition to the ordinary page size. Some listings were
duplicated across pages, and promoted cards made the overall order nonchronological.
There is no useful page-size control to request.

Nineteen listings carried publication timestamps from 2026-08-21 at the time of the
measurement. That supports an estimated matching volume of roughly 15–25 new
internships per day, rather than the site's 50–150 total daily jobs. This is low
enough for per-listing alerts; a digest is not justified.

The HTML exposes absolute ISO-8601 publication timestamps such as
`2026-08-21T21:50:27+02:00`. The explicit offset follows Paris local time and handles
daylight-saving changes. The parser will use that value and never parse localized
relative text such as `il y a une heure`.

### Identity and URLs

The stable identity is the terminal numeric FashionJobs job ID. It appears in both
ordinary detail URLs (`...,12000001.html`) and promoted redirect URLs
(`/redir/12000002,1.html`). The state key is the decimal ID as a string.

Ordinary cards provide a direct FashionJobs detail URL, which becomes the Discord
embed URL. Some promoted cards expose only a site-provided `/redir/` URL. Those cards
must still be monitored; their site-provided URL is included without the monitor
following it. If duplicate appearances of one ID expose both forms, the direct detail
URL wins. Duplicate cards must agree on title, company, location, contract, and
publication time; conflicting core data fails the poll rather than selecting an
arbitrary version.

An edit or reappearance with the same ID does not alert again. A repost with a new ID
does. Listings may be reordered or removed without removing their IDs from state.

### Access and respectful polling

FashionJobs' robots file permits the public listing paths used here and disallows
paths including `/ajax/`, `/flux/`, and `/redir/`. The monitor will not request those
paths. A promoted `/redir/` value may be delivered to Discord for a human to click,
but the crawler will not follow it. No published API rate limit or polling-specific
terms were found.

The default interval will be 600 seconds. The shared runner's existing 20 percent
jitter spreads requests rather than fixing them to exact clock boundaries. In the
usual case this is six page requests per hour. Extra sequential pages are requested
only while the scan is still crossing unseen IDs. A complete 42-page walk is normally
required only when no usable persisted baseline exists or when an explicit empty
baseline asks for the current backlog; an unusually large new frontier can also reach
the final page. This is both timely enough for an internship feed and modest for a
public result page.

Discovery made 14 top-level FashionJobs requests plus one isolated browser page load
and its static resources. It sent no Discord notifications. The browser profile was
deleted after discovery. Sanitized representative responses are committed under
`tests/fashionjobs/fixtures/`; tests will never contact FashionJobs.

The remaining environmental unknown is whether Cloudflare treats the production GCE
egress IP differently. That can be checked with a single container-side read during
operational setup. It does not justify shipping a browser or a bypass. A block would
be reported as a source failure and require a new source decision before deployment.
No current design choice requires further user input.

## Application design

### Components

The new `apps/fashionjobs` workspace package will follow the existing app shape:

- `types.py` defines an immutable fully parsed job and a lightweight known-ID
  placeholder used to keep state monotonic.
- `site.py` owns the fixed URLs, HTTP status validation, HTML parsing, pagination,
  duplicate reconciliation, and transactional observation cache.
- `alert.py` converts new jobs into one shared-library `Message` per ID.
- `monitor.py` adapts the source to the shared `Monitor` protocol.
- `config.py` loads typed operational configuration while fixing the product filters.
- `main.py` wires one `httpx.AsyncClient`, the app, and the shared runner.

The shared runner and parity harness remain unchanged. FashionJobs has no independent
TypeScript implementation, so adding it to parity would compare nothing.

### Read and pagination flow

On process startup, the app reads the existing shared state loader once to seed the
FashionJobs source's observation frontier. The runner still independently owns the
authoritative baseline, first-run decision, notification settlement, and state writes.
The source's observation cache is not persisted state.

Each source read is transactional:

1. Request page 1 with a fixed transparent user agent and timeout.
2. Require a successful HTML response, the expected Stage route and filter markers,
   structurally complete job cards, and coherent pagination links.
3. Parse all cards on the page. Exclude and log valid cards whose contract is not
   exactly `Stage`; never skip malformed cards.
4. Deduplicate by numeric job ID and reconcile permitted ordinary/promoted URL
   differences.
5. If there was no usable startup baseline, follow every sequential page through the
   declared final page. Otherwise, continue while the current page contains at least
   one ID not in the pre-read observation frontier. Stop at the first page containing
   only observed IDs or at the declared final page.
6. Only after every required page succeeds, merge the candidate jobs into the
   in-memory observation cache and return both parsed jobs and placeholders for every
   retained ID.

The retained placeholders prevent reorder, disappearance, or incremental pagination
from shrinking the runner's key set. The heartbeat count therefore means “FashionJobs
listing identities seen,” not “listings still live on the site.” The cache retains the
full record for jobs observed during the current process so a failed Discord post can
be rendered again even if the card disappears before the next poll.

An explicit state file containing `[]` remains the shared runner's supported request
for the current backlog. Because every page then contains unseen IDs, the source walks
the full result set and the runner alerts it in deterministic order. A missing state
file instead triggers the same complete read but the runner establishes a silent
baseline.

### Deterministic ordering and delivery

The monitor returns a single logical item per ID. Before rendering, new jobs are
sorted ascending by `(published_at, numeric_job_id)`. Thus multiple jobs first seen in
one poll are announced oldest first with a numeric tie-breaker, independent of page
order, promotions, or duplicate appearances.

Each `Message` covers exactly one state key. The runner posts messages sequentially
and adds a covered key to the returned baseline only after that message succeeds. If
message N fails, successfully delivered earlier jobs stay settled while that job and
all later unposted jobs remain fresh for the next tick. A render failure withholds the
entire fresh batch, matching the existing invariant.

The Discord embed will contain:

- title: job title;
- URL: direct detail URL, or the card's site-provided promoted URL;
- fields: company, location, contract (`Stage`), and publication time;
- publication time rendered from the absolute timestamp using Discord's timestamp
  syntax so it displays in the reader's locale;
- a short FashionJobs source footer.

HTML entities are decoded once. User-controlled Markdown characters are escaped and
Discord field/title limits are enforced without breaking an escape sequence. The
payload sets `allowed_mentions.parse` to an empty list, so even source text containing
`@everyone` cannot notify a role. There is no `MENTION_EVERYONE` option for this app.

## Failure and retry semantics

The following conditions fail the entire source read:

- transport errors, timeouts, non-2xx statuses, or a non-HTML response;
- an unexpected canonical/filter route or missing checked Stage marker;
- malformed required job fields, invalid IDs, invalid absolute timestamps, or
  conflicting duplicate data;
- unsafe, off-origin, cyclic, skipped, or otherwise incoherent pagination links;
- failure of any page required by the current traversal;
- a claimed nonzero result set with no valid Stage cards.

No candidate IDs are merged into the observation cache on a failed read, and the
runner receives no empty snapshot to persist. Network errors, HTTP 429, and 5xx
responses are retryable source failures. Other 4xx responses are treated as permanent
for diagnostic wording but still fail closed; the shared loop will continue probing
at its failure cadence because an endpoint or access policy can recover. There is no
tight in-request retry loop. The shared runner supplies retry cadence, exponential
failure backoff, liveness/death recovery, and jitter.

A legitimate empty result is accepted only when the response still proves the exact
Stage filter, declares zero results, contains no cards, and has no next page. Existing
retained IDs remain in the returned set. An unreadable or malformed state file remains
distinct from an empty array through the existing state loader; the shared runner
silently re-baselines only because it has no recoverable keys and sends the existing
high-priority corruption status alert.

## Configuration and operations

FashionJobs product filtering has no environment variables. Operational configuration
will use the shared schema and established Terraform naming:

- required per-app Discord webhook;
- optional separate status webhook;
- `STATE_PATH=/data/state.json`;
- `POLL_INTERVAL_SEC`, default 600;
- `HEARTBEAT_AT`, default `07:00` in `America/Denver`;
- shared heartbeat interval and stall threshold;
- optional health-server fields left at their shared disabled defaults.

Terraform will validate positive poll intervals, webhook values through the existing
patterns, and an empty or 24-hour `HH:MM` heartbeat time. The example tfvars will show
placeholder webhook URLs only. `infra/terraform.tfvars` will not be read or changed.

The generic Dockerfile and generic systemd/startup templates already support another
workspace app. The implementation will add a `fashionjobs` app entry, its image and
memory cap, its environment map, Terraform variables, example configuration, and
documented log commands. Nothing will be deployed, applied, pushed, or restarted.

## Repository changes

Planned changes are limited to:

- `apps/fashionjobs/` for the typed package and entry point;
- root `pyproject.toml` and `uv.lock` for the workspace member;
- `tests/fashionjobs/` for sanitized fixtures and offline tests;
- `infra/apps.auto.tfvars`, `infra/variables.tf`, `infra/main.tf`, and
  `infra/terraform.tfvars.example` for the third service;
- `README.md`, `apps/README.md`, `infra/README.md`, `docs/architecture.md`, and
  `docs/invariants.md` for behavior and operations.

The Dockerfile, systemd templates, shared monitor library, parity script, and sibling
TypeScript repositories should not change. A shared-code change would require a newly
demonstrated mismatch and a separate explanation before implementation.

## Invariants

All existing shared invariants apply unchanged, especially:

- a missing baseline suppresses first-run alerts while an unusable baseline reports
  corruption;
- source/read failures cannot become successful empty snapshots;
- notification-covered keys are withheld until delivery succeeds;
- multiple messages settle independently and retain deterministic order;
- state writes are atomic, and a write failure keeps the in-memory baseline without
  misreporting a poll failure;
- retry/backoff, heartbeat scheduling, liveness transitions, jitter, and graceful
  shutdown remain runner-owned.

FashionJobs adds these app-specific invariants:

1. Every page must prove the exact `Stage` filter and remain on the France Stage route.
2. A valid non-Stage card is never alerted; a malformed card is never silently
   skipped.
3. A required multi-page read commits no observation-cache changes until all required
   pages succeed.
4. Duplicate IDs alert once; conflicting duplicate core data fails the read.
5. Retained identity placeholders keep state monotonic across reorder, removal, and
   incremental pagination.
6. Promoted cards are covered without crawling their robots-disallowed redirect URLs.
7. Multiple new jobs are ordered by absolute publication time and numeric ID.
8. A same-ID edit or reappearance does not alert; a new-ID repost does.

## Test and verification design

All source tests use committed fixtures and `httpx.MockTransport`; no test may contact
the live site. Coverage will include:

- representative ordinary and promoted Stage extraction;
- non-Stage exclusion and exact France-wide request construction;
- absolute Paris-offset timestamps without parsing localized relative text;
- sequential pagination, duplicate IDs across pages, and canonical URL preference;
- reordered and removed results with monotonic retained IDs;
- multiple new jobs with deterministic alert order;
- missing-state baseline, empty-state backlog, and restart deduplication;
- notification failure followed by successful retry;
- transport, retryable-status, permanent-status, malformed-response, and structural
  source failures;
- partial pagination failure with no partial cache/state success;
- legitimate empty results versus failed reads;
- Discord fields, Markdown escaping, limits, URL selection, and disabled mentions;
- heartbeat/status tracked-item count.

High-value guards will receive explicit mutation evidence during implementation. At
minimum, temporarily removing Stage equality, malformed-card failure, required-page
completion, retained placeholders, and deterministic sorting must make their focused
tests fail. Notification withholding remains covered by both the existing runner tests
and a FashionJobs integration test. Temporary mutations will be reverted before
commits.

The final gate run will report the baseline of 301 tests and the final count, each
requested command's exit status, Docker and Terraform static validation, any skipped
parity prerequisite, live discovery traffic, mutation evidence, and confirmation that
nothing was deployed.

## Resource impact

The production addition is one idle Python 3.13 process, one `httpx` connection pool,
and a small HTML/identity cache. It adds no browser, database, inbound listener, or
background worker. A 128 MiB container limit is the initial target, subject to a local
container measurement before the infrastructure commit. Existing declared caps are
128 MiB for Melanzana and 256 MiB for Jeffco, so a 128 MiB FashionJobs cap keeps the
sum at 512 MiB and leaves capacity for Container-Optimized OS, Docker, logging, and
normal bursts on the roughly 1 GiB `e2-micro`. CPU and network use should be negligible
between ten-minute polls.
