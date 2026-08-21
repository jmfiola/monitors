# Jeffco Sub Monitor — Python Port Design

**Status:** design only, nothing implemented. `lib/monitor` is live and
`apps/melanzana` runs on it in production as `v2.0.1`. This is the second
consumer, and the one the library's seams were validated against without ever
having been used by.

## Goal

Port `jeffco-sub-monitor` from TypeScript to Python on `lib/monitor`, so all
monitors share one runtime and one loop. The library was deliberately designed
against jeffco's requirements; this cycle tests whether that worked.

Success is narrow and checkable: jeffco keeps alerting a real person about real
jobs, with no missed alert attributable to the port, and the shared library needs
one small addition rather than a redesign.

## Scope

**In scope**

- `apps/jeffco` — the port.
- One library change: a `"busy"` health outcome (see [Why the library changes
  once](#why-the-library-changes-once)).
- Anonymizing the four test fixtures, in **both** repos, consistently.

**Not in scope**

- **Rebuilding melanzana.** A library change cannot reach it until its image is
  rebuilt, and that image is pinned to `v2.0.1`. Rebuilding is a separate,
  deliberate step after jeffco is stable, gated on the existing 132 tests and the
  melanzana differential harness.
- **Hoisting jeffco's lockout guard into `timing.py`.** The previous spec deferred
  this "until a second app needs it", and jeffco is that app — so the deferral
  formally expires here, and the answer is still no. See [The lockout guard stays
  in the app](#the-lockout-guard-stays-in-the-app).
- **Coalescing `format_delivery_failure` per tick.** Real (jeffco posts one message
  per job, so a 20-job batch hitting a payload rejection posts 20 identical ops
  messages) but it fires only when Discord rejects a payload outright, and the fix
  touches the `run_tick` ops path that took two review rounds to settle. Deferred
  with the reason recorded.
- The fashionjobs app, and the Secret Manager migration for instance metadata.

## What jeffco stops owning

Measured against the current TypeScript: `timing.ts` (17 lines), `state.ts` (43),
`health.ts` (36), and the loop half of `index.ts` all disappear into the library —
roughly 400 of 1,500 source lines and 41 of 173 tests deleted rather than
translated.

The library supplies: the poll loop, the key-set diff, first-run suppression,
jitter and backoff, health folding, heartbeat and stall/recovery messages, webhook
transport, inter-message spacing, post-failure withholding, key de-duplication, and
the heartbeat footer override that exists specifically for jeffco's filter-gap
report.

What remains is genuinely jeffco's:

```
apps/jeffco/src/jeffco/
├── types.py       Job, JobDay
├── schools.py     the HS filter: folds, pattern, partition, gap collection
├── dates.py       day/time labels, worked-day parsing, the degraded fallback
├── sfe.py         the source client: login, token cache, lockout ceiling, busy signal
├── alert.py       one message per job, plus the heartbeat's gap fields and footer
├── config.py      JeffcoConfig, LABELS, load_config
├── monitor.py     JeffcoMonitor — the four contract methods
└── main.py        wires a Monitor to run_forever()
```

## The three hard parts

### Dates, and why not `strftime`

`Intl.DateTimeFormat('en-US', …).formatToParts` produces `"Fri Oct 16"` and
`"7:45 AM"`. The obvious Python translation is `strftime`, and it is wrong twice:
`%a`/`%b` are locale-dependent, and `%-I` (hour without a leading zero) is a glibc
and BSD extension that Python's docs explicitly decline to guarantee.

Instead reuse the construction melanzana's `alert.py` already proved byte-identical
against TypeScript: explicit `WEEKDAYS` and `MONTHS` tuples, `zoneinfo` for the
zone, and the hour, minute, and meridiem assembled by hand. That pattern has a
byte-for-byte differential result behind it, which no `strftime` format string does.

**America/Denver stays pinned in code**, as it is today, for the reason the previous
spec gives: an env var for a zone only creates a way to typo an identifier and raise
on every tick forever. jeffco's `Config.timezone` field stays a field rather than a
constant, though — the existing tests pass `'UTC'` to prove the date code honours the
zone it is given rather than the host's, and that property is worth keeping.

**The DST-spanning job is the one case where a real divergence is plausible.** A job
running Friday into the following Monday across a transition renders two day labels
whose wall-clock times differ by an hour from naive arithmetic. `zoneinfo` and
`Intl` are both IANA-backed, so they *should* agree — but "should" is what a fixture
is for. This gets its own fixture case and its own harness case.

### Authentication

All of it stays instance state on the client in `sfe.py`: the token cache, the JWT
`exp` read, the 120-second refresh margin tuned to a 7200-second TTL, and the
failure ceiling. This is precisely why the library's contract made `Monitor` a class
rather than free functions.

`is_account_busy` maps SFE's HTTP 400 to the library's `SourceBusy`, raised out of
`fetch()`. The runner then holds the poll cadence and leaves the backoff ladder
where it was, instead of doubling the gap at the moment someone is on the site
claiming the job the last alert announced.

### Per-job enrichment in `render()`

`render()` is async in the contract for exactly this: the list response's
`daysOfWeek` cannot express a Friday-and-following-Monday job, so the authoritative
per-day array is fetched per new job, after the diff.

A failed detail fetch must degrade **that one job's** date line to the approximate
form. This matters more in Python than it did in TypeScript: under the library, a
`render()` raise is treated as "these items were not announced", which withholds
every fresh key in the batch. One unreachable detail endpoint must not silence a
whole tick's alerts.

## Why the library changes once

`SourceBusy` currently folds as a health `"failure"`. jeffco raises it routinely —
34 times in the monitor's first ~59 hours — so an SFE session outlasting
`STALL_ALERT_SEC` (600s) posts *"No successful poll since… the monitor may be
blocked or down"* about an upstream that is demonstrably reachable, when the actual
cause is the account holder using their own account.

The tempting change is a third outcome that simply does not feed `is_stalled` at
all. **That is wrong, and it recreates a bug this project just fixed in
production.** If a busy tick never counts toward the stall check, then an SFE that
answers 400 *permanently* — broken, or the account genuinely locked — produces
permanent silence under a heartbeat still reporting "still watching". That is the
same shape as the Cloudflare-5xx defect: an error class quietly reclassified as
benign, so the monitor stops reporting the thing it exists to report.

So the change is narrower and has two parts:

- **A busy tick does not reset `last_success_unix`.** The stall clock keeps running;
  a busy period is still an absence of data, and pretending otherwise is what
  produces silence.
- **A busy-only absence gets its own, longer threshold and its own wording.**
  `BUSY_STALL_ALERT_SEC` (default 3600) applies while *every* failure since the last
  success was a busy signal; the moment any other fault occurs, the normal
  `STALL_ALERT_SEC` (600) governs again. The alert says the account appears to be in
  use elsewhere, rather than claiming the monitor may be blocked or down.

That keeps all three properties: an hour-long legitimate session does not page
anyone, a permanently-400ing SFE still alerts (after an hour, with an honest cause),
and a real fault still alerts in ten minutes.

melanzana never raises `SourceBusy`, so its behaviour is unchanged by construction —
and it cannot observe the change at all until its image is rebuilt.

## The lockout guard stays in the app

A wrong credential retried on a 60-second cadence is ~1,440 attempts a day, and this
account belongs to a real person who would lose access to the thing the monitor
watches. So the ceiling is not optional. But it is not general either: it counts
*login* failures specifically, and its recovery window is a property of SFE's
lockout policy, not of poll loops. Hoisting it would give the library a
failure-ceiling abstraction with one caller and one API's semantics baked in — the
same argument that kept authentication out of the library in the first place.

`timing.py` keeps carrying the hazard as a comment, which is what makes the next
authenticated app notice it.

## Parity strategy

### Fixtures are anonymized, in both repos, before anything else

The four fixtures carry real district employees — names and employee IDs of the
absent teachers the substitute covers for. Byte-for-byte reuse is what made the
melanzana port trustworthy, and applying that rule unchanged would copy real
people's data into a second repo.

So: replace every name and employee ID with stable fakes in
`jeffco-sub-monitor`'s fixtures **and** the Python copies, identically, and update
the TypeScript expectations that quote them. The two copies stay byte-identical, so
`cmp` still enforces the rule and the differential harness still works. This is a
one-time edit to a working repo, done while its tests are being read anyway.

School names stay real: they are public institutions, they are already shipped in
`DEFAULT_HS_SCHOOLS` as code, and the filter's whole behaviour depends on their
exact spellings.

### Four layers, cheapest first

1. **Anonymize and copy the fixtures**, `cmp`-verified in both directions.
2. **Translate the surviving tests**, tests before implementation. Counted with
   vitest, never grepped — an earlier revision of this paragraph carried three wrong
   counts, each of which would have silently under-ported a file:

   | File | Cases | Fate |
   | --- | --- | --- |
   | `timing` | 7 | dropped, library-owned |
   | `state` | 5 | dropped, library-owned |
   | `health` | 5 | dropped, library-owned |
   | `index` | 24 | dropped, library-owned (the loop half) |
   | `schools` | 41 | translated |
   | `sfe` | 40 | translated |
   | `discord` | 21 | translated |
   | `config` | 16 | translated |
   | `dates` | 14 | translated |

   41 dropped, 132 translated, 173 total. Several of config's 16 cover shared names
   (`STATUS_WEBHOOK_URL`, `HEARTBEAT_INTERVAL_SEC`, `STALL_ALERT_SEC`) that
   `tests/lib/test_config.py` already asserts, so those become app-level wiring
   checks rather than re-tests of library primitives. Likewise `discord`'s
   ops-message cases (heartbeat, death, recovery, `postAlert`) are library-owned.
   Plus new tests for the four contract methods and for the busy-stall threshold.
   Expect roughly 110.

   **Enumerate each file's cases before porting it.** Every under-port in this cycle
   came from working off a remembered count or an illustrative excerpt instead of
   opening the file and listing its `it(...)` blocks.
3. **Extend the differential harness.** A jeffco dump on both sides: job alerts
   rendered from all four fixtures, a heartbeat **with and without** filter gaps —
   that pair is the footer-override seam the library added for jeffco and has never
   been exercised against the TypeScript — plus death and recovery, on a frozen
   clock. `cmp` the fixtures first, as melanzana's does.
4. **One deliberate live authenticated smoke**, at a quiet hour, before cutover.

### No shadow run, deliberately

melanzana's fourth layer was a 48-hour shadow run. Here it is hazardous rather than
merely expensive: SFE answers HTTP 400 while a second session is active on the
account, so a second instance manufactures the exact failure it is meant to detect,
during the hours jobs appear. There is no "nothing is open right now" window either —
a missed job is a lost day's work.

The offline harness covers the risky part (rendering), the 40 translated `sfe` tests
cover the auth state machine with mocked HTTP, and the one thing neither can answer —
whether real SFE accepts the Python client's login and request shape — needs exactly
one authenticated call, not a 48-hour parallel session.

**Cut over at a quiet hour**, so a surprise is cheap, with the rollback path that is
now rehearsed in both directions.

## Expected divergences from jeffco's current behaviour

All five are inherited from the library and all are improvements. They are listed so
the parity criteria can carve them out explicitly rather than have a test fail and
look like a regression.

1. **Per-message withholding.** jeffco withholds *every* fresh key when any post in
   a batch fails, because it has no per-message handle. `Message.covers` gives the
   runner one, so only the unlanded message's keys are withheld.
2. **Payload-rejected vs webhook-refused.** A 400/413/422 banks the keys and posts
   an ops message; a 401/403/404 withholds and logs loudly. jeffco has neither
   distinction.
3. **Any 5xx is retryable**, including Cloudflare's 520-524. jeffco's current
   handling would treat them as faults and escalate backoff.
4. **Key de-duplication.** jeffco keys on `jobId` alone, so duplicates are unlikely,
   but the library de-duplicates regardless.
5. **The `"busy"` outcome**, above — the point of the library change.

## Error handling

| Failure | Response | Owner |
| --- | --- | --- |
| SFE HTTP 400 (account busy) | hold cadence, no ladder escalation, no false death alert | app raises `SourceBusy`, library handles |
| Login rejected | count it; stop trying past the ceiling | app |
| Detail fetch fails for one job | degrade that job's date line to approximate | app |
| Any other fetch failure | back off; do not touch the baseline | library |
| Alert post fails | withhold or bank per status; never re-raise | library |
| State write fails | log, continue on the in-memory baseline | library |
| `heartbeat_extras()` raises | send a plain heartbeat | library |

## Testing

The same gates as melanzana: `pytest`, `mypy --strict`, `ruff`, and a
`tools/parity-diff.sh` that must diff empty. Added for this port: a DST-spanning job
fixture, exercised in both the unit tests and the harness.

## Deployment

jeffco keeps its `apps.auto.tfvars` key, its Artifact Registry repository, and its
image name; only the tag moves, to a Python `v2.0.0`. `HEARTBEAT_AT` becomes
meaningful for jeffco and gets wired.

`BUSY_STALL_ALERT_SEC` is a library-side default (3600) and is deliberately **not**
wired into Terraform this cycle, for the same reason the previous spec withheld
`HEARTBEAT_AT` until the library read it: a variable that nothing consumes is config
that silently does nothing. Wire it only if an hour turns out to be the wrong
threshold in practice.

**The memory cap stays at `256m` for the first deploy.** melanzana came in at 25 MiB
against Node's 94, and the temptation is to drop jeffco to `128m` immediately.
Predicting a footprint is the mistake the previous spec made — it guessed ~35 MB and
was wrong in the pessimistic direction. Measure first, lower afterwards.

## Deferred

- Coalescing `format_delivery_failure` per tick (above).
- Hoisting the lockout guard (above, and argued against).
- Enforcing Discord's 1024-character field and 6000-character embed caps in the
  library. jeffco's `MAX_GAP_NAMES = 10` exists precisely because an uncapped
  accumulating gap list would eventually make the heartbeat itself unpostable; that
  cap ports as-is, and the general enforcement waits for a third consumer.
- Rebuilding melanzana onto the changed library.
- An external dead-man's-switch, unchanged from the previous spec: a resident app
  reports its own inability to poll, but a dead process or a dead VM is invisible.
