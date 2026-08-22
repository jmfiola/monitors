# Architecture

How the monitors are built. For running and deploying them see the
[root README](../README.md); for adding one see [`apps/README.md`](../apps/README.md);
for the host see [`infra/README.md`](../infra/README.md). Rules that must not be
"simplified" away, and the tests that hold them, are in [invariants.md](invariants.md).

## The shape

Every monitor does the same thing: poll a website, work out what is new since last
time, and post it to Discord. That loop is identical across apps, so it lives in
`lib/monitor` exactly once. What differs — where the data comes from, what counts as
new, and how an alert reads — lives in the app.

```
lib/monitor/src/monitor/
├── types.py     Monitor protocol, Payload/Embed/Field/Message, HeartbeatExtras,
│                OpsLabels, SourceBusy, the three embed colours
├── config.py    env_* readers, RunnerConfig, ConfigError
├── state.py     load_state / save_state — the seen-key baseline
├── timing.py    system_now, with_jitter, next_backoff
├── health.py    HealthState, should_alert_stall, should_heartbeat
├── discord.py   post(), heartbeat / status embeds, retry classification
└── runner.py    run_tick and run_forever — the loop

apps/melanzana/src/melanzana/     apps/jeffco/src/jeffco/
├── types.py                      ├── types.py     Job, JobDay
├── cowlendar.py  source client   ├── sfe.py       source client + auth
├── detector.py   month + slot    ├── schools.py   the high-school filter
│                 selection       ├── dates.py     day/time labels
├── alert.py      day-card embed  ├── alert.py     one message per job
├── config.py                     ├── config.py
├── monitor.py    the 4 methods   ├── monitor.py   the 4 methods
└── main.py       wiring          └── main.py      wiring

apps/fashionjobs/src/fashionjobs/
├── types.py      FashionJob, KnownJob, FashionItem
├── site.py       fixed Stage HTML parser + paginated identity frontier
├── alert.py      one message per listing
├── config.py
├── monitor.py    the 4 methods
└── main.py       wiring
```

## The Monitor contract

Four methods, deliberately not six. `Item` is whatever the app models — a slot, a
job, a posting. The library never inspects it; it only passes it back to `key` and
`render`.

```python
class Monitor[Item](Protocol):
    async def fetch(self) -> list[Item]: ...        # may raise SourceBusy
    def key(self, item: Item) -> str: ...
    async def render(self, new: list[Item]) -> list[Message]: ...
    def heartbeat_extras(self) -> HeartbeatExtras: ...
```

A class rather than free functions, because an app may hold per-instance state:
jeffco keeps a token cache, a refresh margin, a login-failure counter and an
accumulated filter-gap set.

**`render` is async and may perform I/O.** jeffco fetches per-job detail *after* the
diff, because the list response cannot express a job that runs Friday and the
following Monday. Enrichment is therefore necessarily post-diff.

**`Message.covers`** names the item keys a message announces. jeffco and FashionJobs
post one message per job or listing, so when a single post fails the runner must
know which keys to withhold. melanzana's one message covers every fresh key, so
the same shape serves all three.

**`heartbeat_extras` returns fields *and* an optional footer.** jeffco's heartbeat
replaces the footer when its filter finds unrecognised schools, so a bare
`list[Field]` could not express it.

**Clock convention.** An implementation that needs the time takes
`now_unix: Callable[[], int]` in its constructor, and `main.py` passes the *same*
callable to the monitor and to `run_forever`. Nothing enforces this, so a test that
freezes one and not the other produces results that look inexplicable.

## What belongs where

**The library owns** state persistence, the key-set diff, first-run suppression,
jitter and backoff, health folding, heartbeat and stall/recovery messages, Discord
transport and retry classification, the status-webhook fallback, inter-message
spacing, post-failure withholding, and key de-duplication.

**The app owns** its source client, its key function, its alert rendering, its
filtering, and any authentication.

The shared Discord client surface is a structural `Protocol`, not a concrete
HTTP-client type or dependency. The library owns one copy of Discord delivery and
its credential-safety rules; each app owns and supplies the client used by its
source and webhook wiring.

FashionJobs therefore owns its HTML parsing and its fixed product rule: contract
exactly `Stage`, anywhere in France, every role and category, with no keyword,
title, company, region, department, or city filtering. Those product rules are not
environment settings.

Two things are outside the library on purpose:

- **Filtering.** `fetch()` returns only the items worth tracking, which keeps the
  contract at four methods instead of six.
- **Authentication.** jeffco is the only app with any, and the parts that look
  generic are entangled with one API: the expiry comes from a JWT `exp` claim and
  the 120-second refresh margin is tuned to a 7200-second token. An auth
  abstraction would have exactly one caller.

## The loop

`run_forever(monitor, cfg)` loads state, fetches, keys, diffs, suppresses on first
run, renders, posts with spacing, saves, folds the outcome into health, emits
heartbeat and liveness messages, then sleeps with jitter or backs off.

### FashionJobs reads are transactional and monotonic

FashionJobs reads the canonical HTML route
`https://fr.fashionjobs.com/fr/contrat/Stage,5.html`; subsequent pages use
`/fr/contrat/Stage,5,<page>.html`. The parser proves on every required page that the
canonical URL is the exact expected Stage route, structured contract filter ID `5`
is checked, the declared Stage count agrees with usable cards, pagination stays on
the FashionJobs HTTPS origin and exact route, and every card has a numeric FJOB ID,
title, company, location, contract, timezone-aware absolute publication timestamp,
and on-origin URL. A malformed card, response, next link, or required page fails the
whole read; it cannot become a successful empty poll or partially advance identity
state.

Only titled links that already validate as canonical `/emploi/` or `/redir/` job
routes may set the title and stable ID. Known `/fr/recrutement/` links remain company
links even when titled, and conflicting supported job links fail the card rather than
letting document order choose an identity.

Parser completion means balanced HTML depth and no unfinished card, capture, or
heading state. Each card must expose exactly two semantic metadata fields: contract
and location. Exactly one timezone-aware absolute `time-ago[data-value]` is required.
Only that element's localized descendant display text is ignored; empty or sibling
semantic metadata still counts and fails the exact shape guard, except that a
structurally present location slot may contain blank text and is then omitted from
the alert. Void timestamp elements and nested metadata wrappers fail immediately.
Contract labels are limited to `Stage`, `CDI`, `CDD`, `Alternance`, `Intérim`, and
`Free-lance`:
recognized non-Stage cards are deliberately excluded and logged, while an unknown
label or metadata shape fails closed. Every declared end, including a later
promotion, is capped by `MAX_PAGES=100` before it extends traversal.

FashionJobs can expose extra promoted cards on page 1: the first declared end may
therefore be lower than a later end reached through the validated sequential next
links. The source promotes that bound and completes the expanded walk, but a lower
later end still aborts the read. A pagination-free positive page is valid only when
page 1 self-proves that its declared result count equals its visible unique IDs
(duplicate appearances do not add IDs), or when the source has already passed that
exact final page number to the parser. A zero-result page 1 remains valid. Any other
pagination-free positive page, a final-page next link, and any malformed or
unexpected next link remain failures.

Discovery on 2026-08-21 observed 1,266 active Stage listings across 42 pages and
estimated roughly 15–25 new matching listings per day. These are point-in-time
observations, not permanent inventory or arrival-rate guarantees. The estimated
volume is why one timely message per listing remains reasonable instead of a digest.

Shared state files are JSON arrays of strings; any non-string element makes the whole
file corrupt. FashionJobs further requires every key to convert safely to a positive
integer and round-trip to the identical canonical decimal string. Whitespace, signs,
leading zeroes, zero, negatives, non-decimals, or integers too large for Python's
guarded conversion all invalidate the whole state. `main.py` validates once: the
source receives that validated state's keys and the runner receives the same
`LoadedState` for first-run and corruption decisions. Invalid state therefore takes
the existing loud-log, silent-rebaseline path without source/runner disagreement.

Every newly constructed FashionJobs source makes one full transactional startup
scan through the first page's declared end, even with seeded state. Only missing or
corrupt state becomes a silent first-run baseline. An explicit empty state is a
deliberate, valid non-first-run baseline: the startup scan treats every current
listing as fresh and alerts the current backlog. Other seeded state makes the startup
scan a duplicate-free safety check. Only a successful full transaction clears the
startup requirement and records the monotonic completion time. A safety scan becomes
due once at least 86,400 monotonic seconds have elapsed and runs on the next poll.
Failed startup or due scans commit neither candidate identity state nor the
completion marker, so they remain due.

FashionJobs may render responsive pagination controls twice. Anchor `rel` values
are case-insensitive token sets, so repeated `next` or `end` declarations are
accepted only when every declaration has the same valid Stage URL; missing or
conflicting declarations fail closed before the transaction can commit.

Ordinary intervening ticks read pages in order and stop at the first page containing
no ID unseen before that read. The retained ID set only grows, and a listing that
disappears from current HTML remains represented by a `KnownJob` placeholder. That
makes restarts duplicate-free and makes the heartbeat's tracked count mean “listing
identities seen,” not “cards on the last page read.”

Ordinary and promoted cards with the same numeric ID reconcile to one identity. Their
core fields must agree, and a direct `/emploi/` URL wins over `/redir/`; the redirect
listing URL is retained when it is the only one present but is never crawled to find
identity. After a successful read, newly observed full records are sorted by
`(published_at, job_id)` and each produces one Discord message on that successful
poll.

At the 600-second interval, an ordinary quiet frontier normally requests only page
1: about six FashionJobs result-page requests per hour, spread by 20 percent jitter.
Add up to one declared full walk every 86,400 seconds in a successful process and one
on every process restart. Exceptional turnover may also keep the fast scan crossing
unseen IDs. If a required page fails, recovery starts again at page 1 and repeats the
still-due scan against unchanged state. The 100-page ceiling bounds unexpected
network and parsing work without weakening fail-closed recovery.

### Health has three outcomes

`success`, `failure`, and `busy`. `busy` is a failure the source *answered* — for
jeffco, SmartFindExpress returns HTTP 400 while the account holder's own session is
active, which happens routinely during the day.

A busy tick **does not** reset `last_success_unix`. A busy period is still an absence
of data, and pretending otherwise is what produces silence. Instead, while *every*
failure since the last success has been busy, the absence is judged against
`busy_stall_alert_sec` (3600) rather than `stall_alert_sec` (600), and the alert names
the cause instead of claiming the monitor may be down. The moment any non-busy fault
occurs, the 600-second threshold governs again.

This keeps three properties at once: an hour-long legitimate session pages nobody, a
permanently-400ing source still alerts, and a real fault still alerts in ten minutes.
See [invariants.md](invariants.md) — the obvious simplification here reintroduces a
silent-monitor bug.

### A failed Discord post is an alerting problem, not a polling problem

The runner catches it, withholds that message's `covers` keys from the saved
baseline, banks every other key, keeps polling at normal cadence, and records the
tick as a **success** for health purposes. The next tick re-alerts the withheld
items.

Failures are classified rather than lumped:

| Status | Meaning | Response |
| --- | --- | --- |
| any 5xx, 408, 425, 429 | transient | retry, then withhold and re-alert next tick |
| 400, 413, 422 | the payload is bad | bank the keys, post an ops message |
| 401, 403, 404 | the webhook is wrong | withhold, log loudly |

Banking a refused *webhook* would discard every slot while the ops message reporting
it went to the same dead endpoint, which is why those two rows differ.

For FashionJobs, each message covers exactly its numeric listing ID. The embed title
is the listing title, the URL is its FashionJobs link, and the fields appear as
`Company`, optional non-empty `Location`, `Contract`, then `Published` with absolute
Discord time only; it does not include Discord's changing relative-time style. Its
description and the redundant FashionJobs France footer are omitted. Source Markdown
is escaped, field limits are respected, and
`allowed_mentions.parse` is empty, so source text such as `@everyone` cannot ping.
If delivery fails retryably, that ID remains outside the saved baseline and is retried
on the next successful delivery; delivered IDs and other safely banked IDs still
commit.

`KnownJob` is identity-only degradation, not an alert. FashionJobs rendering omits
those placeholders. The runner's existing uncovered-key guard logs and withholds only
their keys, while valid full jobs in the same batch still post and settle. A committed
fixture integration exercises the real `FashionJobsSource` through
`FashionJobsMonitor` and `run_tick`, including retained identities and one new alert.

### The heartbeat lands at a wall-clock hour

`HEARTBEAT_INTERVAL_SEC` alone anchors the heartbeat to process start, so it arrives
at whatever time the last deploy happened, re-anchors on restart, and creeps later
daily. `HEARTBEAT_AT` (`HH:MM`, America/Denver) fixes it: due when the local date has
changed since the last heartbeat *and* the local time is at or past it. Both
conditions are needed — the date check is what stops it firing repeatedly all day.

Unset, the interval behaviour applies. The infrastructure config uses `07:00` for
all three apps: early enough to read over coffee and ahead of most of the day's
postings.

The zone is pinned in code rather than configurable. Making it an env var only creates
a way to typo an identifier and raise on every tick forever. It is the operator's
clock, not the watched site's.

## Configuration

The library reads `DISCORD_WEBHOOK_URL`, `STATUS_WEBHOOK_URL`, `STATE_PATH`,
`POLL_INTERVAL_SEC`, `POLL_JITTER_PCT`, `HEARTBEAT_INTERVAL_SEC`, `HEARTBEAT_AT`, and
`STALL_ALERT_SEC`. Each app adds its own on top — jeffco `SFE_USER_ID`, `SFE_PIN`,
`HS_SCHOOLS`, `WINDOW_DAYS`; melanzana its calendar id and `MENTION_EVERYONE`.
FashionJobs adds only operational settings: its webhook, state path, 600-second poll
interval, shared 20 percent jitter, and `07:00` heartbeat. Stage, France, all roles,
and no keywords remain fixed in code.

`busy_stall_alert_sec` and `HTTP_TIMEOUT_SEC` are code constants, not env vars,
because nothing has yet needed to change them per environment. A variable nothing
consumes is config that silently does nothing.

Config is validated once at startup and refused loudly. A missing webhook exits
rather than polling forever with nowhere to report.

## Testing

```bash
uv run pytest -q                                  # 422
uv run mypy --strict lib apps tests tools
uv run ruff check . && uv run ruff format --check .
./tools/parity-diff.sh                            # needs node + the sibling repos
```

`pytest` covers the library and all three apps against committed fixtures, on pinned
epochs — never a live clock, so a test cannot pass only until the first of the month.

**`parity-diff.sh` is the strongest Melanzana/Jeffco check.** Those two apps have
TypeScript reference implementations in sibling repos. The harness renders every
Discord payload each implementation can produce, on a frozen clock, as canonical
JSON, and the diff must be empty. It catches what unit tests miss: field ordering,
number formatting, the
`allowed_mentions` pairing, and the exact en dash in a time range.

FashionJobs has no TypeScript reference implementation and remains outside the parity
harness. Its exact HTML, source, transaction, and Discord behavior is held by the
committed Python fixtures and focused tests.

It is not part of `pytest` because it needs those sibling repos and a node toolchain.
What makes an empty diff meaningful, and what must never be added to it, is in
[invariants.md](invariants.md).

## Deployment

One `e2-micro` on Container-Optimized OS in the `cobs-cloud` project, each app a
container under its own systemd unit, images from Artifact Registry, each app's
`state.json` on a host-path volume so it survives restarts.

One root `Dockerfile` builds every app via `--build-arg APP=<name>`. Everything else
per app is derived from its key in `infra/apps.auto.tfvars`: unit and container name
`<app>-monitor`, volume `/var/lib/<app>-data`, env file `/etc/monitors/<app>.env` at
`0600`. Adding an app is a three-line tfvars diff.

FashionJobs has a 128 MiB container cap. Together with Melanzana's 128 MiB and
Jeffco's 256 MiB, declared app caps total 512 MiB on the roughly 1 GiB `e2-micro`,
leaving capacity for the OS, Docker, logging, and normal bursts. Between its
ten-minute polls it adds only an idle Python process and small HTTP/HTML identity
state; there is no browser, database, inbound listener, or worker.

Melanzana and Jeffco remain on `v2.0.2`. The first FashionJobs `v2.1.0` rollout
proved GCE egress and Discord delivery but exposed that the localized timestamp
display text can be empty even though the absolute `data-value` is present. A
one-page `v2.1.1` smoke then exposed titled recruitment links surrounding the real
job link. `v2.1.2` passed its focused smoke, but its first baseline exposed the
promoted/final-page pagination shape. All published images remain immutable;
`v2.1.3` remains undeployed after its structural smoke exposed identical responsive
pagination anchors. `v2.1.4` contained the focused pagination fix, but its first
baseline failed closed, before state or alerts, on a structurally present but blank
location. `v2.1.5` preserves the required location slot while allowing blank text
and omitting the unavailable field from Discord alerts. Its production rollout
completed a 42-page baseline, persisted 1,242 unique identities, and sent no
first-run alerts. `v2.1.6` keeps the fixed publication date while removing Discord's
changing relative-time suffix and the redundant FashionJobs France footer. Its
rollout retained the baseline, delivered one newly observed listing, and persisted
1,243 unique identities.

Deploy with `./infra/deploy.sh` — never a bare `terraform apply`, which is half a
deploy that looks complete. Details in [`infra/README.md`](../infra/README.md).

## Known gaps

- **Secrets are in instance metadata.** `infra/main.tf` base64-encodes each env file
  into the `startup-script` metadata value, and base64 is not encryption. Anyone with
  `compute.instances.get` on `cobs-cloud` can read every webhook and the SFE PIN.
  On-host they are `0600` root-only. Closing it means Secret Manager, with the startup
  script fetching each app's config using the VM service account.
- **No external dead-man's-switch.** An app reports its own inability to poll, but a
  dead process or a dead VM is invisible, and silence is indistinguishable from
  "nothing new". Apps log nothing on a quiet tick, so a hung loop and an idle loop
  look identical in the logs.
- **Discord's 1024-character field and 6000-character embed limits are not
  enforced centrally.** Each app caps its own content.
- **`format_delivery_failure` posts per message.** For an app that posts one message
  per item, a batch hitting a payload rejection posts one identical ops message per
  item.
