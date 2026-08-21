# Jeffco Port — Outcome

Companion to [the plan](2026-08-21-jeffco-port.md) and
[the design](../specs/2026-08-21-jeffco-port-design.md). Written when the work merged, so
the reasoning survives the scratch workspace it was recorded in.

**Shipped:** `apps/jeffco` (the jeffco substitute-teaching monitor ported from TypeScript
to `lib/monitor`), one library change, and a differential parity harness spanning both
repos. Deployed as `jeffco-sub-monitor:v2.0.1`.

## Final state at merge

| | |
| --- | --- |
| Tests | 299 (was 132 after the melanzana cycle) |
| Types | `mypy --strict` clean, 52 files |
| Lint | `ruff check` and `ruff format --check` clean |
| Parity | byte-identical, 7720 bytes both sides |
| melanzana parity | still byte-identical, 7833 / 7308 — unchanged by the library change |
| TypeScript repo | 173 vitest passing, `tsc --noEmit` clean, no dependencies added |
| Resident memory | **25.8 MiB** on the host, against Node's **100.2 MiB** |

## Acceptance criteria, against the spec

| Criterion | Result |
| --- | --- |
| ~110 pytest tests green; `mypy --strict` and `ruff` clean | **Met** — 299 total, 167 added this cycle |
| Differential dump byte-identical, including a heartbeat with and without gaps | **Met** — the footer-override seam is exercised against the TypeScript for the first time |
| Fixtures anonymized in both repos, `cmp`-verified | **Met** — 5 files including `login-page.html` |
| One live authenticated smoke | **Met** — authenticated first try; the 400 that followed was the predicted session collision |
| Cut over at a quiet hour | **Waived by decision.** Cut over midday instead, because jeffco's baseline was `[]` — nothing open to re-announce or mis-suppress — and the deploy was watched live |
| Rollback exercised | **Met** — both directions, plus a stronger offline test (below) |
| No missed alert attributable to the port | **Met** as far as is observable: `firstRun=False`, and the filter reported the same gap the TypeScript did |

## The library changed once, and narrowly

`SourceBusy` used to fold as a health `"failure"`. jeffco raises it routinely — 34 times in
its first ~59 hours — so an SFE session outlasting `STALL_ALERT_SEC` posted *"the monitor
may be blocked or down"* about an upstream that was demonstrably reachable.

The tempting fix was a third outcome that does not feed `is_stalled` at all. **That would
have recreated a bug this project had just fixed in production:** an SFE answering 400
*permanently* would produce permanent silence under a heartbeat still saying "still
watching" — the same shape as the Cloudflare-5xx defect, where an error class was quietly
reclassified as benign and the monitor stopped reporting the thing it exists to report.

So instead: a busy tick **does not** reset `last_success_unix`, and a busy-*only* absence
gets its own longer threshold (`BUSY_STALL_ALERT_SEC`, 3600) and its own wording. The
moment any non-busy fault occurs, the normal 600s threshold governs again. All three
properties hold: an hour-long legitimate session pages nobody, a permanently-400ing SFE
still alerts, and a real fault still alerts in ten minutes.

`BUSY_STALL_ALERT_SEC` is deliberately **not** wired into Terraform. A variable nothing
consumes is config that silently does nothing — the same reason `HEARTBEAT_AT` was withheld
last cycle until the library actually read it.

## What the parity harness is, and why it is trustworthy

The cycle deliberately skipped a shadow run: SFE answers HTTP 400 while a second session is
active, so a second instance manufactures the exact failure it is meant to detect, during
the hours jobs appear. The harness replaces it.

The property that makes an empty diff mean something: **no expected output is written down
on either side.** Every literal in both dump scripts is an *input* — Job fields, epochs,
clock integers, fixture names — or an import from production code, and every rendered byte
comes from each language's own production functions. `LABELS` is *imported* from
`jeffco/config.py` on one side and lives in production `src/discord.ts` on the other, so
even config wording drift is caught rather than masked. The 14 gap names are *generated*
independently in each language rather than transcribed.

Verified to be capable of failing, three times over: en dash → hyphen (11 hunks, exit 1);
reversing per-job message order (8 hunks — this is what proves `canon` preserves array
order, which sorted keys could otherwise hide); `MAX_GAP_NAMES` 10 → 9. It cannot print a
diff and exit 0, a crashing dump script cannot fake a pass (`set -e` aborts before `diff`),
and the output is byte-identical under `TZ=` Denver, UTC, Tokyo and Kiritimati.

### Two constraints on the harness, both load-bearing

- **There is no busy case, and adding one would be a mistake.** The TypeScript has no busy
  Discord payload at all — `discord.ts:168` is its only stall message and its busy handling
  is a log line at `index.ts:367`. A busy case could only be built by having the TypeScript
  fabricate the Python library's new wording, which prints one literal twice and proves
  nothing, or by accepting a permanent diff, which destroys the meaning of an empty one. A
  parity harness must exclude *deliberate* divergences. The wording is unit-tested at
  `tests/lib/test_discord.py:169`.

- **Case 5's `not-a-date` must stay unparseable.** Measured:

  ```
  Date.parse("not-a-date") -> NaN            fromisoformat -> ValueError
  Date.parse("2026-02-30") -> 1772409600000  fromisoformat -> ValueError
                              (silently 2026-03-02)
  ```

  Garbage is rejected by both, so both degrade and the bytes match. A well-formed
  *impossible* date is rolled over by JavaScript and rejected by Python, so "strengthening"
  the fixture would produce a divergence that reads as a port regression and is really the
  previous cycle's documented deliberate one. (melanzana's equivalent case differed because
  it built dates component-wise with `Date.UTC(y, m, d, 12)`, which rolls over
  unconditionally, rather than parsing an ISO string.)

- **One serializer difference exists.** A 30-value probe found 29 byte-identical and exactly
  one disagreement: a whole-valued float prints `3.0` in Python and `3` in Node. No float
  reaches a payload today (`uptime_hours` uses `//`), so it is latent — but the first one
  that does will produce a mystery diff. Recorded at the top of the dump script. The
  direction is at least fail-safe: Node can never emit `3.0`.

## The lockout guard, proven against the live service

A wrong credential retried on a 60-second cadence is ~1,440 attempts a day, against an
account belonging to a person who gets work through that site. The ceiling is the app's most
important safety property, and unit tests only ever exercised it against mocked transports.

Run in a real container with a deliberately fake user id, against real SFE, backoff doubling
60 → 120 → 240 → 300s:

```
login failed 3x; pausing login attempts for 3600s so a bad credential cannot lock the account
SFE login suppressed after 3 consecutive login failures; not retrying for another 3359s
  (protecting the account from lockout)
```

The second message carries **no body size** — because no request was made. The guard
suppresses the network call itself, not merely the success.

**It stays in the app, and the deferral for hoisting it into `timing.py` formally expired
this cycle with the answer still no.** It counts *login* failures specifically and its
recovery window is a property of SFE's lockout policy, not of poll loops. Hoisting it would
give the library a failure-ceiling abstraction with one caller and one API's semantics baked
in. `timing.py` keeps carrying the hazard as a comment, which is what makes the next
authenticated app notice it.

## Rollback: what was actually proven

Rehearsed in both directions on the host — v2.0.0 → v1.1.0 gave Node `firstRun=false`, and
v1.1.0 → v2.0.0 restored Python.

**That rehearsal alone was weak evidence**, because production state was `[]` and an empty
array proves almost nothing about format compatibility. The real property was tested offline
with non-empty content:

```
Python wrote ["1025548", "1026739", "998877"]  (sorted, spaces)     -> Node read all 3
Node   wrote ["1026739","1025548","998877"]    (insertion, no ws)   -> Python read all 3
```

Cosmetically different, mutually readable, and order is not behaviour because both load into
a set. That is the property a rollback actually depends on.

## Defects found in this cycle's own work

Recorded because the pattern matters more than the individual bugs: **every one was found by
a reviewer or an implementer disagreeing with the plan, not by the plan being right.**

- **The `Location` header leak.** httpx parses `Location` even with `follow_redirects=False`,
  and its `RemoteProtocolError` quotes the header — which for SFE legitimately carries a live
  `;jsessionid=`. An implementer found it; I then found the guard covered only **two of four**
  send sites and reproduced the leak through the public API. All four now funnel through one
  `_send` with enumerated keyword parameters, so a caller structurally cannot pass
  `follow_redirects=True`. A reviewer then drove 59 failure paths against 7 needles across 5
  channels including `traceback.format_exception`, and could not leak anything.
- **`sorted()` would have defeated the gap report.** My brief said
  `heartbeat_extras_for(sorted(self._unmatched_seen))`. The formatter keeps `[-10:]`, so
  sorting turns "the newest ten" into "the alphabetically last ten" and a newly discovered
  campus early in the alphabet never appears. Caught by an implementer cross-referencing an
  earlier task's test.
- **My own regression test for that was vacuous.** It failed against unmutated code exactly
  as it failed against the mutation. Cause: `partition_jobs` returns `sorted(unmatched)` from
  a set, so intra-tick order is alphabetical regardless — and my test packed 13 names into one
  tick, where "discovery order" is a fiction. Rewritten to pin the property that is real:
  cross-tick order. Had I not insisted on seeing the mutation fail, I would have committed a
  test that passes for the wrong reason and reads as protection.
- **A swapped-argument bug in the live smoke script**, found before it ran.
  `JeffcoMonitor.__init__` is `(cfg, sfe, log, now_unix)`; my script passed
  `(cfg, sfe, system_now, log)`. It fails at the first log call — partway through login —
  spending one of the three attempts the account gets per hour. `mypy` could not catch it
  because the script lives inside a `python -c` string. Now keyword arguments throughout.
- **Real employee names survived in a tracked file.** An earlier scrub caught three documents
  but missed the plan itself, which carried the full real→fake mapping — making the fixture
  anonymization trivially *reversible* while the key sat two directories away. The mapping's
  real column is now a description, the maps are empty with an assert forcing them to be
  filled from a scratch file outside the repo, and the sweep step now covers both repos'
  whole tracked trees rather than `test/` and `src/`.
- **Three factual errors in my briefs**, each caught and corrected: `apps/README.md` has no
  table (the table is in the root `README.md`); `"Doral Academy of Colorado"` is *already* in
  `DEFAULT_HS_SCHOOLS`, so the additive test's `+1` was wrong; and case `3-multiday`'s fixture
  has two days with *identical* hours, so it took the uniform branch and was not pinning
  per-day times at all.

## Known gaps, deliberately not closed

- **Secrets are still in instance metadata.** Unchanged from the previous cycle and verified
  again: `main.tf` base64-encodes each env file into the `startup-script` metadata value, and
  base64 is not encryption. Treat `compute.viewer` on `cobs-cloud` as equivalent to holding
  every monitor's credentials. Secret Manager is the fix and it is its own change.
- **The gap-report cap is count-based, not character-based.** `MAX_GAP_NAMES = 10` protects
  against unbounded accumulation, but ten pathologically long school names could still exceed
  Discord's 1024-character field limit and make the heartbeat unpostable. Realistic names
  reach ~350–400 characters. The original TypeScript test has the identical weakness. The
  failure would be visible rather than silent — a Discord 400 routes to the library's
  payload-rejected ops path.
- **`apps/jeffco/.env.example` was not created.** A permission rule denies writing `.env*`,
  and it denies reading melanzana's too, so neither an implementer nor I could write it and
  neither of us should work around a deliberate deny rule. Documentation only: the deploy
  reads config from `apps.auto.tfvars` and the metadata env files.
- **Real names remain in `jeffco-sub-monitor`'s git history** (9 commits). The repo is
  private. Rewriting history needs a force-push, so it stays the owner's call.
- **`format_delivery_failure` is still posted per message.** jeffco posts one message per
  job, so a 20-job batch hitting a payload rejection posts 20 identical ops messages. Real,
  but it fires only when Discord rejects a payload outright and the fix touches the `run_tick`
  ops path that took two review rounds to settle.
- **melanzana has not been rebuilt onto the changed library.** Its image is pinned to
  `v2.0.1`, so it cannot observe the busy outcome yet. Gated on its own 132 tests and its
  differential harness.
- **No external dead-man's-switch.** Unchanged, and this cycle made it concrete: jeffco logs
  nothing on a quiet tick, so a hung loop and a quiet loop are indistinguishable in the log.
  Proving the loop alive took a cgroup CPU delta (12 ms over 70 s) and a connection count. A
  resident app cannot report its own death.

## Two measurement facts worth keeping

- **A local Docker Desktop memory reading is inflated by the VM.** jeffco read 42.47 MiB
  locally and 26.86 MiB on the host; melanzana read 43.9 and 25.3. Measure on the host.
  The `256m` cap can drop to `128m` next cycle — measured, not predicted, which was the point
  of leaving it alone this time.
- **A local image size is not a registry size.** jeffco's image is 185 MB locally, which looked
  like a 4× regression against melanzana's recorded 42.28 MB — until melanzana's *local* image
  also measured 185 MB. The 42.28 MB was a compressed figure. Only 7.81 MB is app code; the
  rest is `python:3.13-slim`. Compare like with like.

## What the final whole-branch review changed

Verdict was safe to merge with no Critical findings. It verified by execution the two
claims worth not taking on faith: melanzana's blast radius is nil (`busy_only` can only be
set by `outcome=="busy"`, which only the `except SourceBusy` handler produces, and
melanzana has zero occurrences of `SourceBusy`), and "a permanently-400ing SFE still
alerts" holds and is *bounded* — busy-forever alerts at exactly t=3600 with the busy
wording, busy-then-one-real-fault at t=840 with the death wording, fault-first at t=600.

Shipped as `v2.0.1`:

- **A partial row drop is no longer silent.** `parse_jobs` tolerates a malformed row on
  purpose so one bad row cannot lose a whole poll, and it raises when *every* row fails.
  In between, a job with a null `jobEnd` was never announced, never logged, and could not
  reach the heartbeat's gap report either, because that only covers rows that parsed —
  every signal read healthy while a real job went unmentioned. Fixed at the call site, so
  `parse_jobs` stays pure, and log-only, so it cannot affect the parity diff. Inherited
  verbatim from `sfe.ts:202`, so not a port regression.
- **`format_approximate` catches `OverflowError`/`OSError` too.** Unreachable today, but
  this is the *degraded* path: anything escaping it propagates out of `render()`, which
  withholds every fresh key and retries the identical failure forever.
- **`busyStall` is in the startup log.** It governs jeffco's commonest failure and is
  deliberately not in Terraform, so the log was the only place it could be seen.

One finding was deliberately **not** acted on: a naive `jobStart` is interpreted in the
host timezone. That looks like a bug and is what makes the two implementations agree —
both JavaScript and Python treat a naive datetime as local. Forcing UTC would *create* a
divergence. Recorded in the design doc so nobody "corrects" it.
