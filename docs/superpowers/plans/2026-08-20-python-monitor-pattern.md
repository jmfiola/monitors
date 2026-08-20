# Python Monitor Pattern Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `lib/monitor` (the shared Python poll-and-alert library) and port `melanzana-monitor` from TypeScript to `apps/melanzana` on top of it, proven at parity by a byte-for-byte differential harness, then deploy it to the live `cobs-cloud` host.

**Architecture:** The library owns the loop — state, diff, first-run suppression, jitter/backoff, health folding, ops messages, webhook transport, spacing. An app supplies four methods (`fetch`, `key`, `render`, `heartbeat_extras`) and its own alert rendering. Every seam in the library was measured against both existing TypeScript apps; three of them (`Message.covers`, `heartbeat_extras`, async `render`) exist because of jeffco specifically, so porting jeffco later does not force a redesign.

**Revision 2** (same day): reviewed against three independent lenses — KISS/YAGNI, architecture, and the cost of adding app #3. The corrections that changed the design are recorded in place: `heartbeat_extras` replacing `heartbeat_fields`, the retryable/permanent split in post-failure handling, a structural transport Protocol instead of a hardcoded `httpx`, and two verified defects in the live `infra/` deploy path.

**Tech Stack:** Python 3.13, `uv` workspace, `httpx`, `pytest` + `pytest-asyncio`, `ruff`, `mypy --strict`, Docker (`python:3.13-slim`), Terraform.

**Spec:** [`docs/superpowers/specs/2026-08-20-python-monitor-pattern-design.md`](../specs/2026-08-20-python-monitor-pattern-design.md) — read it before Task 1. The plan argues from it.

## Global Constraints

- **Python 3.13** on `python:3.13-slim`. Not alpine: musl breaks some wheels, and `curl_cffi` will need manylinux when fashionjobs arrives.
- **`mypy --strict` and `ruff` clean** at the end of every task. No `Any` leaks across a module boundary.
- **Tests before implementation**, every task. A test that has never been seen to fail proves nothing.
- **`uv` for everything.** `uv run pytest`, `uv run mypy`, `uv run ruff`. Never a bare `pip` or `python`.
- **The heartbeat's zone is pinned to `America/Denver` in code**, never read from the environment. Making it an env var only creates a way to typo an identifier and raise on every tick forever.
- **`HEARTBEAT_AT` intended value is `07:00`**, wired into Terraform in Task 15 — after the library reads it, not before.
- **No secret in a log line, an error message, or instance metadata.** A Discord webhook's path *is* its credential; config errors get logged.
- **All melanzana date arithmetic stays UTC** (`datetime.UTC`), matching the TypeScript's `getUTCFullYear` / `Date.UTC(..., 12)`. The one exception is `HEARTBEAT_AT`, which is the operator's clock.
- **Deploy only with `./infra/deploy.sh`.** Bare `terraform apply` is half a deploy that looks complete: GCE does not re-run the startup script on a metadata change.
- **`jeffco-sub-monitor` stays TypeScript and untouched.** It is load-bearing for a real person's work. Its Terraform entry, image, and unit are not modified by this plan.
- **The fixture `availability-sample.json` is copied byte-for-byte and never regenerated.** 663 bytes. Task 13 enforces this with `cmp`.

## Divergences from melanzana's current behaviour

All three are deliberate. Everything else is parity.

1. **Post-failure semantics (from the spec).** A failed Discord post no longer propagates out of the tick. The runner withholds the affected message's `covers` keys from the saved baseline, banks every other key, keeps polling at normal cadence, and records the tick as a **success** for health purposes. Melanzana today enters backoff and records a health failure, which drives a sustained Discord outage toward a stall alert delivered over the same broken Discord. With more than one message per tick this can re-send an already-delivered message; a duplicate costs one glance, a swallowed slot can cost a day's work.

   **Retryable and permanent failures are separated**, which the spec does not address. Withholding assumes the next tick can succeed. A permanent rejection — a payload over Discord's 6000-char total embed cap, reachable with 24 day-cards after the documented `echo '[]' > state.json` re-baseline — would otherwise be retried forever while the tick folds as success and the heartbeat reports "still watching": silence, indefinitely. So `429`/`5xx`/network → withhold and retry, and a `429` also aborts the rest of the batch rather than hammering a rate-limited webhook. Any other `4xx` → bank the keys so the retry stops, and post an ops message naming the status code. The item is still lost, but a human is told.

2. **State-write-failure semantics (decided during planning, adopting jeffco's).** A failed `save_state` is logged and the loop continues on the in-memory baseline, rather than being reported as a poll failure. Melanzana today lets it propagate into backoff plus a health failure. Jeffco's reasoning, which applies unchanged: a write failure reported as a *poll* failure throttles polling 5×, latches a false death alert, and suppresses the heartbeat — all while alerts are arriving normally. The in-memory baseline is what the process runs on; durability is best-effort, and a restart re-baselines from disk exactly as before. Asserted in Task 8.

3. **Fresh items are de-duplicated by key (a bug fix).** Melanzana's month enumeration pads and therefore overlaps, and neither `filterBookable` nor `detectNew` de-duplicates, so one slot returned by two month queries alerts as "3 open slot(s)" for a single slot with its day-card line repeated three times. The TypeScript has the identical bug. The library de-duplicates in `run_tick`, which fixes it for every app. Invisible unless the duplicate case occurs.

**Not covered by parity:** log line wording (per the spec), and config error message wording where the library adopts jeffco's stronger URL validation (Task 5).

## Known gaps, recorded rather than built

These are real and deliberately deferred. Written down so the jeffco cycle does not rediscover them.

- **`SourceBusy` still folds as a health `"failure"`.** An SFE session outlasting `STALL_ALERT_SEC` (600s) produces a death alert saying the monitor "may be blocked or down" when the upstream is reachable and the cause is the account holder using their own account. This is parity with jeffco today and the spec is silent; melanzana never raises `SourceBusy`, so it cannot affect this cycle. The minimal fix when jeffco ports is a `"busy"` outcome that does not feed `is_stalled`.
- **No enforcement of Discord's 1024-char field cap or 6000-char embed cap.** Jeffco's `MAX_GAP_NAMES = 10` exists solely so an accumulating list cannot make the heartbeat itself unpostable. Each app caps its own content today. Clamping in the library would silently truncate and would itself break the differential harness; validating and raising is the better shape, when there is a second consumer to justify it.
- **`POST_SPACING_SEC` is a dataclass default, not an env var.** Jeffco's 0.35s against Discord's ~2.5 req/s leaves almost no margin, but nobody will tune this from an env file; it is a code constant with a reason attached.
- **The redesign risk, named:** an alert keyed on *removals* — "that slot got taken". The diff is computed and consumed entirely inside `run_tick`, and `render(new)` never sees the previous key set or what vanished from it. Adding that means changing `render`'s signature in every app. A `Batch(new, gone)` wrapper would hedge it for one line today; not built, because no requirement asks for it and the wrapper would be an abstraction with one shape and no second one in sight.
- **`Message` has no target webhook**, so per-message routing (one channel per filter) means changing `run_tick`'s use of `cfg.alert_webhook_url`. Same reasoning as above.

## Waived this cycle

- **The 48-hour shadow run** (spec parity layer 4, and one of its acceptance criteria). Skipped by decision during planning. Layers 1–3 — byte-for-byte fixture, ~50 translated tests, the differential dump — all ship. The cost of skipping: divergence that only appears against live Cowlendar data, over a real day's slot churn, is not caught before production. Mitigation is Task 16's post-deploy log watch and the rehearsed rollback.

## File Structure

```
monitors/
├── pyproject.toml                        virtual uv workspace root: members, dev deps, tool config
├── uv.lock
├── Dockerfile                            ONE image recipe for every app, selected by ARG APP
├── .dockerignore
├── lib/monitor/
│   ├── pyproject.toml                    name = "monitor" — depends on tzdata, NOT on httpx
│   └── src/monitor/
│       ├── __init__.py
│       ├── types.py                      Monitor protocol / Message / HeartbeatExtras / Payload /
│       │                                 Embed / Field / OpsLabels / SourceBusy / GREEN,RED,BLUE
│       ├── timing.py                     with_jitter / next_backoff / system_now  (+ lockout hazard)
│       ├── state.py                      load_state / save_state
│       ├── health.py                     HealthState / init_health / is_stalled / should_heartbeat
│       ├── config.py                     env_* primitives / RunnerConfig / load_runner_config / make_log
│       ├── discord.py                    post / format_heartbeat / format_status_alert /
│       │                                 format_delivery_failure
│       └── runner.py                     run_tick / run_liveness / run_forever / shutdown handlers
├── apps/
│   ├── README.md                         the add-an-app checklist
│   └── melanzana/
│   ├── pyproject.toml                    name = "melanzana" — owns its httpx dependency
│   └── src/melanzana/
│       ├── __init__.py
│       ├── types.py                      Slot
│       ├── config.py                     MelanzanaConfig / LABELS / load_config
│       ├── cowlendar.py                  source client
│       ├── detector.py                   months_to_fetch / filter_bookable
│       ├── alert.py                      day-card embed
│       ├── monitor.py                    MelanzanaMonitor — the four contract methods
│       └── main.py                       wires a Monitor to run_forever()
├── tests/
│   ├── lib/                              test_types / test_timing / test_state / test_health /
│   │                                     test_config / test_discord / test_runner
│   └── melanzana/
│       ├── fixtures/availability-sample.json     copied byte-for-byte
│       └── test_cowlendar / test_detector / test_alert / test_config / test_monitor
├── tools/
│   ├── dump_payloads.py                  Python side of the differential harness
│   └── parity-diff.sh                    runs both dumps, cmps the fixture, diffs
└── infra/                                already live — Task 15 touches 4 lines
```

**Two deviations from the spec's illustrative tree, both packaging mechanics rather than seams:**

1. `src/` layout inside each workspace member (`lib/monitor/src/monitor/state.py`, not `lib/monitor/state.py`). The alternative that matches the tree literally puts the package at `lib/monitor/monitor/`, which shadows its own name on `sys.path`. Module names, responsibilities, and the contract are exactly as specified.
2. `detector.py` exists in melanzana alongside `cowlendar.py` and `alert.py`. It holds `months_to_fetch` and `filter_bookable`, mirroring the TypeScript `detector.ts` so the translated tests keep a 1:1 file mapping with the suite they came from. That mapping is a parity aid; folding them into `cowlendar.py` would make the translation harder to review for no gain.

**Two contract notes:**

1. The spec writes `heartbeat_fields()` with `# default: []`. Structural typing has no defaults — a `Protocol` method body is not inherited by a structural implementer. The protocol declares the method and melanzana implements the three-line body. The alternative (a `hasattr` probe in the runner) trades a typed contract for an untyped one.

2. **The method is `heartbeat_extras() -> HeartbeatExtras`, not `heartbeat_fields() -> list[Field]`.** This corrects the spec. Jeffco's heartbeat does not merely add a field when its filter finds unrecognised schools — it *replaces the footer*, `"Routine heartbeat — no action needed."` → `"If any of these are high schools, add them to HS_SCHOOLS."` The field carries the school names; the footer carries the thing to do about them. A `list[Field]` return cannot reach the footer, so the seam that exists specifically for jeffco would not have fit jeffco, and the spec's claim that these three seams prevent a later redesign would have been false for this one. `HeartbeatExtras(fields, footer_text)` is twelve lines now against a contract break across every app later. Three independent reviewers found this; it is the single most valuable correction in this revision.

## Two tool facts that bit during execution

Both were defects in this plan's literal test code, found by implementers and fixed here.

- **`mypy --strict` implies `--no-implicit-reexport`.** Reaching through a module to
  something it imported — `monitor.state.os`, `monitor.runner.save_state` as an attribute —
  fails with `does not explicitly export attribute`. Use `monkeypatch`'s **string form**
  (`monkeypatch.setattr("monitor.state.os.replace", ...)`), which mypy does not type-check
  as an attribute access. Do not "fix" it in the library with `import os as os`: that puts
  a test-driven idiom into production code where the next tidy-up will silently break the
  test.
- **ruff's isort groups `monitor` and `melanzana` with third-party imports**, because both
  are installed into the venv by `uv sync` rather than found on a source path. The import
  blocks written out in this plan are not authoritative about grouping — if `ruff check`
  reports `I001`, run `uv run ruff check --fix .` and take its ordering.

## Verification commands

Every task ends with these green:

```bash
uv run pytest -q
uv run mypy --strict lib apps tests tools
uv run ruff check .
uv run ruff format --check .
```

---

### Task 1: Workspace scaffolding and library types

The payload types come first because every later task consumes them, and because their one non-obvious behaviour — absent keys are *omitted*, never `null` — is the single thing most likely to break the differential harness in Task 13.

**Files:**
- Create: `pyproject.toml`, `lib/monitor/pyproject.toml`, `apps/melanzana/pyproject.toml`, `.dockerignore`
- Create: `lib/monitor/src/monitor/__init__.py`, `lib/monitor/src/monitor/types.py`
- Create: `apps/melanzana/src/melanzana/__init__.py`
- Test: `tests/lib/test_types.py`

**Interfaces:**
- Consumes: nothing.
- Produces, all in `monitor.types`:
  - `Field(name: str, value: str, inline: bool = True)`, `Embed(title: str, description: str, color: int, url: str | None = None, fields: tuple[Field, ...] | None = None, footer_text: str | None = None)`, `Payload(embeds: tuple[Embed, ...], content: str | None = None, allowed_mentions_parse: tuple[str, ...] | None = None)` — each with `.to_dict() -> dict[str, Any]`.
  - `Message(payload: Payload, covers: tuple[str, ...])`, `HeartbeatExtras(fields: tuple[Field, ...] = (), footer_text: str | None = None)`, `OpsLabels(name: str, tracked_noun: str, death_footer: str)`, `SourceBusy(Exception)`.
  - `GREEN = 0x2ECC71`, `RED = 0xE74C3C`, `BLUE = 0x3498DB`. **The colours live here, not in `discord.py`.** `melanzana.alert` needs `GREEN` and nothing else; importing it from `discord` would drag the transport and the runner's config schema into a pure renderer.
  - `class Monitor[Item](Protocol)` — the contract. **It lives here, not in `runner.py`.** Every symbol it mentions is a `types` symbol, and putting it in the loop's own module means an app cannot type-check against the contract without importing the loop.

- [ ] **Step 1: Create the workspace root**

`pyproject.toml`:

```toml
# A virtual workspace root: no [project] of its own, just the members, the dev
# toolchain, and one place for tool config. `uv sync` installs every member.
[tool.uv.workspace]
members = ["lib/monitor", "apps/melanzana"]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "mypy>=1.13",
    "ruff>=0.8",
    # The library itself does not depend on httpx (see lib/monitor/pyproject.toml);
    # the tests use it as a concrete client to exercise the transport Protocol.
    "httpx>=0.28",
]

[tool.pytest.ini_options]
# auto mode: an `async def test_` needs no per-test decorator. The runner and its
# tests are async throughout, so the decorator would be on nearly every test.
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "--strict-markers"

[tool.ruff]
line-length = 100
target-version = "py313"
# ruff 0.8 formats Python code blocks inside Markdown. The plan and spec in docs/
# are documents, not source: reformatting them would churn every code sample and
# risks their nested code-fence structure.
extend-exclude = ["docs"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "ASYNC", "SIM", "RUF"]

[tool.mypy]
python_version = "3.13"
strict = true
# The library and app are installed into the venv by `uv sync`, so imports
# resolve without a mypy_path entry.
#
# Deliberately no `files =`: paths are passed on the command line. A fixed list
# would name tools/, which does not exist until Task 13, and mypy errors on a
# missing directory rather than skipping it.
```

`lib/monitor/pyproject.toml`:

```toml
[project]
name = "monitor"
version = "1.0.0"
description = "Shared poll-and-alert monitor library"
requires-python = ">=3.13"
# tzdata, and deliberately NOT httpx.
#
# tzdata: monitor.health builds ZoneInfo("America/Denver") at import time, and
# health is imported by discord and runner. If the base image lacks the system
# tz database that is an import-time ZoneInfoNotFoundError on every start —
# which under Restart=always + StartLimitBurst=5 is a permanently failed unit
# whose only liveness signal is the process that just died. zoneinfo falls back
# to this package automatically, so ~450 KB removes the base-image bet entirely.
#
# No httpx: the whole reason this project is Python is that fashionjobs needs
# curl_cffi with impersonate="chrome" to clear Cloudflare's TLS fingerprinting.
# A library that names httpx in a type annotation forces that app to reimplement
# post() — including the "never put the webhook URL in the error" rule, which is
# the last invariant that should be copy-pasted. The transport is a Protocol; the
# app brings its own client.
dependencies = ["tzdata>=2025.1"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/monitor"]
```

`apps/melanzana/pyproject.toml`:

```toml
[project]
name = "melanzana"
version = "2.0.0"
description = "Melanzana appointment-slot monitor"
requires-python = ">=3.13"
dependencies = ["monitor", "httpx>=0.28"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/melanzana"]

[tool.uv.sources]
monitor = { workspace = true }
```

`.dockerignore` — the image needs the two members and the lock, nothing else:

```
.git
.venv
infra
docs
tests
tools
**/__pycache__
**/.mypy_cache
**/.pytest_cache
**/.ruff_cache

# .env before .env.example: the Dockerfile COPYs whole app directories, and
# .gitignore does NOT cover the build context. A developer's local
# apps/melanzana/.env holds a live Discord webhook, and it would ride into a
# pushed image in Artifact Registry.
**/.env
!**/.env.example
```

Both `__init__.py` files are empty.

- [ ] **Step 2: Install and confirm the workspace resolves**

```bash
cd ~/personal/monitors
uv python pin 3.13
uv sync
uv run python -c "import monitor, melanzana; print('workspace ok')"
```

Expected: `workspace ok`. If `uv` reports the members cannot be found, the `packages = ["src/..."]` line is wrong — hatchling resolves it relative to the member's own `pyproject.toml`.

- [ ] **Step 3: Write the failing test**

`tests/lib/test_types.py`:

```python
from monitor.types import BLUE, GREEN, RED, Embed, Field, HeartbeatExtras, Payload


def test_payload_omits_absent_content_and_mentions() -> None:
    # JSON.stringify drops undefined-valued properties, so emitting null here
    # would diverge from the TypeScript payload on the wire AND in the
    # differential dump. Discord also treats the two differently: a null content
    # is a validation error, an absent one is a normal embed-only message.
    payload = Payload(embeds=(Embed(title="t", description="d", color=1),))
    assert payload.to_dict() == {"embeds": [{"title": "t", "description": "d", "color": 1}]}


def test_payload_includes_the_everyone_pair_when_set() -> None:
    payload = Payload(
        embeds=(Embed(title="t", description="d", color=1),),
        content="@everyone",
        allowed_mentions_parse=("everyone",),
    )
    assert payload.to_dict() == {
        "content": "@everyone",
        "embeds": [{"title": "t", "description": "d", "color": 1}],
        "allowed_mentions": {"parse": ["everyone"]},
    }


def test_embed_omits_url_fields_and_footer_when_absent() -> None:
    assert Embed(title="t", description="d", color=1).to_dict() == {
        "title": "t",
        "description": "d",
        "color": 1,
    }


def test_embed_emits_an_empty_fields_array_when_given_one() -> None:
    # formatAlert always sets `fields`, even when the list is empty, so an empty
    # tuple must render as [] rather than being dropped.
    assert Embed(title="t", description="d", color=1, fields=()).to_dict()["fields"] == []


def test_embed_renders_url_fields_and_footer() -> None:
    embed = Embed(
        title="t",
        description="d",
        color=1,
        url="https://example.test/x",
        fields=(Field(name="n", value="v"),),
        footer_text="f",
    )
    assert embed.to_dict()["url"] == "https://example.test/x"
    assert embed.to_dict()["fields"] == [{"name": "n", "value": "v", "inline": True}]
    assert embed.to_dict()["footer"] == {"text": "f"}


def test_heartbeat_extras_defaults_to_adding_nothing() -> None:
    # The common case: an app with nothing to say. Both halves must be absent, so
    # format_heartbeat can tell "no fields" from "an empty fields array" and can
    # keep its own default footer.
    extras = HeartbeatExtras()
    assert extras.fields == ()
    assert extras.footer_text is None
    assert HeartbeatExtras(footer_text="do this instead").footer_text == "do this instead"


def test_the_colours_are_the_typescript_constants() -> None:
    # Asserted because they cross a module boundary now: melanzana.alert reads GREEN
    # from here, and a payload whose colour changed would fail the parity harness
    # with a diff that points at the wrong place.
    assert (GREEN, RED, BLUE) == (0x2ECC71, 0xE74C3C, 0x3498DB)
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/lib/test_types.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitor.types'`

- [ ] **Step 5: Write the implementation**

`lib/monitor/src/monitor/types.py`:

```python
"""Library types, and the contract an app implements.

`Field`, `Embed`, and `Payload` mirror Discord's webhook and embed-field shapes.
`Item` — whatever an app models — is never one of these: the library only ever
passes an item back to the app's own `key` and `render`.

Everything here is a leaf: this module imports nothing from the rest of the
library, which is what lets an app depend on the contract without pulling in the
loop or the transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

GREEN = 0x2ECC71
RED = 0xE74C3C
BLUE = 0x3498DB


class SourceBusy(Exception):
    """The upstream is busy, not broken.

    Raised by a source to mean "try again at the normal cadence". The runner holds
    its poll interval instead of escalating backoff, and leaves the escalation
    ladder where it was so a real fault arriving later still climbs from where it
    left off.

    Jeffco needs this because SmartFindExpress answers HTTP 400 while the account
    holder's own session is active: over the monitor's first ~59 hours all 34 of
    its 400s landed between 06:00 and midnight, with none across three nights of
    overnight polling. Escalating on those would blind the monitor for minutes
    precisely when someone is claiming the job the alert just announced.
    """


@dataclass(frozen=True)
class Field:
    """One embed field. `inline` defaults true because day-cards are inline."""

    name: str
    value: str
    inline: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "inline": self.inline}


@dataclass(frozen=True)
class Embed:
    title: str
    description: str
    color: int
    url: str | None = None
    fields: tuple[Field, ...] | None = None
    footer_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # Key order mirrors the TypeScript object literals, so an unsorted dump
        # reads the same shape side by side. The parity harness sorts keys anyway,
        # but a human diffing by eye does not.
        out: dict[str, Any] = {"title": self.title}
        if self.url is not None:
            out["url"] = self.url
        out["description"] = self.description
        out["color"] = self.color
        if self.fields is not None:
            out["fields"] = [f.to_dict() for f in self.fields]
        if self.footer_text is not None:
            out["footer"] = {"text": self.footer_text}
        return out


@dataclass(frozen=True)
class Payload:
    embeds: tuple[Embed, ...]
    content: str | None = None
    allowed_mentions_parse: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict. Absent values are OMITTED keys, never `null`.

        `JSON.stringify` drops properties whose value is `undefined`, so emitting
        `"content": null` would diverge from the TypeScript payload both on the
        wire and in the differential dump. Discord also treats the two
        differently: a null `content` is a validation error, an absent one is a
        normal embed-only message.
        """
        out: dict[str, Any] = {}
        if self.content is not None:
            out["content"] = self.content
        out["embeds"] = [e.to_dict() for e in self.embeds]
        if self.allowed_mentions_parse is not None:
            out["allowed_mentions"] = {"parse": list(self.allowed_mentions_parse)}
        return out


@dataclass(frozen=True)
class Message:
    """One webhook post plus the item keys it announces.

    `covers` is the whole reason this type exists. Jeffco posts one message per
    job and, when a post fails, withholds exactly those jobs' keys from the
    baseline so the next tick re-alerts them — which the runner cannot do without
    knowing which keys each message announces. Melanzana's single message covers
    every fresh key, so one shape serves both.
    """

    payload: Payload
    covers: tuple[str, ...]


@dataclass(frozen=True)
class HeartbeatExtras:
    """Whatever an app wants to add to the next heartbeat.

    `footer_text` is not decoration. Jeffco's heartbeat carries the school names
    its filter did not recognise as a field, and *replaces the footer* with "If any
    of these are high schools, add them to HS_SCHOOLS." The field is the data; the
    footer is the thing to do about it. A bare `list[Field]` return cannot reach
    the footer, so the seam built for jeffco would not have fit jeffco.
    """

    fields: tuple[Field, ...] = ()
    footer_text: str | None = None


@dataclass(frozen=True)
class OpsLabels:
    """Per-app wording for the ops messages the library formats.

    Three fields, because three is exactly where the two existing apps' ops
    strings diverge: the monitor's name, the noun it counts ("slot(s)" vs "high
    school job(s)"), and the death alert's footer, which tells the reader what to
    do while the monitor is blind and is therefore app-specific advice.
    """

    name: str
    tracked_noun: str
    death_footer: str


class Monitor[Item](Protocol):
    """What an app must provide. Four methods, deliberately not six.

    `Item` is whatever the app models — a slot, a job, a posting. The library never
    inspects it; it only ever passes it back to `key` and `render`.

    Filtering is not here on purpose: `fetch()` returns only the items worth
    tracking, so jeffco's High-School predicate and its gap logging stay inside
    jeffco. Authentication is not here either — jeffco is the only app with any, and
    the pieces that look generic are entangled with one API's JWT.

    A class rather than free functions because both apps hold per-instance state:
    jeffco a token cache, a refresh margin, a lockout counter and an accumulated
    filter-gap set; melanzana none today.

    **Clock convention.** An implementation that needs the current time takes a
    `now_unix: Callable[[], int]` in its constructor, and `main.py` passes the SAME
    callable to the monitor and to `run_forever`. `fetch()` takes no argument (the
    spec's signature), so nothing enforces this — a test that freezes one clock and
    not the other will produce results that look inexplicable.
    """

    async def fetch(self) -> list[Item]:
        """Every item worth tracking, right now.

        May raise `SourceBusy` to mean "the upstream is busy, not broken", which
        holds the normal cadence instead of escalating backoff. Any other exception
        is a fault: the runner backs off and does not touch the baseline, because no
        data is not the same as "nothing available".
        """
        ...

    def key(self, item: Item) -> str:
        """The item's stable identity. This is what the baseline stores."""
        ...

    async def render(self, new: list[Item]) -> list[Message]:
        """Build the messages announcing `new`.

        Async, and allowed to perform I/O: jeffco fetches per-job detail after the
        diff to build each alert's date line, so enrichment is necessarily
        post-diff. Prefer degrading one message over raising — a raise here is
        treated as "these items were not announced", so they are withheld and
        retried next tick.

        Every fresh key should appear in some message's `covers`. Any that does not
        is withheld rather than banked, because a key banked without being announced
        is an item silently swallowed forever.
        """
        ...

    def heartbeat_extras(self) -> HeartbeatExtras:
        """Extra content for the next heartbeat.

        The library decides *when* a heartbeat is due; the app supplies anything
        extra. Called only when one is actually due, and a raise degrades to empty
        rather than propagating — an app-side error here must not take down the loop
        that reports the app is alive.
        """
        ...
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/lib/test_types.py -q && uv run mypy --strict lib tests && uv run ruff check . && uv run ruff format --check .`
Expected: 7 passed, mypy `Success`, ruff `All checks passed`. If `ruff format --check` fails, run `uv run ruff format .` and re-check — formatting is not a review-worthy decision.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock .dockerignore lib apps tests
git commit -m "Add the uv workspace and the library's payload types"
```

---

### Task 2: timing.py — jitter and backoff

Byte-identical between the two TypeScript apps, so this is a pure translation. It also carries the lockout hazard comment, which is the one piece of this module that is not obvious until it bites.

**Files:**
- Create: `lib/monitor/src/monitor/timing.py`
- Test: `tests/lib/test_timing.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `with_jitter(base_sec: float, jitter_pct: float, rand: Callable[[], float]) -> float`, `next_backoff(current_sec: float, base_sec: float, max_sec: float) -> float`, `system_now() -> int`, `MAX_BACKOFF_SEC: float = 300`.

`system_now` lives here so there is exactly one of it. Defining it in both `runner.py` and each app's `monitor.py` is how the two clocks drift apart in a test.

- [ ] **Step 1: Write the failing test**

`tests/lib/test_timing.py` — the seven assertions translated from `melanzana-monitor/test/timing.test.ts`:

```python
import pytest

from monitor.timing import MAX_BACKOFF_SEC, next_backoff, with_jitter


def test_with_jitter_returns_base_at_the_midpoint() -> None:
    assert with_jitter(10, 20, lambda: 0.5) == 10


def test_with_jitter_subtracts_the_full_percentage_at_zero() -> None:
    # 20% of 10 = 2; rand 0 => -2
    assert with_jitter(10, 20, lambda: 0.0) == pytest.approx(8)


def test_with_jitter_adds_the_full_percentage_at_one() -> None:
    assert with_jitter(10, 20, lambda: 1.0) == pytest.approx(12)


def test_with_jitter_never_returns_below_one_second() -> None:
    assert with_jitter(1, 100, lambda: 0.0) >= 1


def test_next_backoff_doubles_the_current_delay() -> None:
    assert next_backoff(10, 10, 300) == 20


def test_next_backoff_caps_at_max() -> None:
    assert next_backoff(200, 10, 300) == 300


def test_next_backoff_starts_from_base_when_there_is_no_prior_backoff() -> None:
    assert next_backoff(0, 10, 300) == 10
    assert MAX_BACKOFF_SEC == 300
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/lib/test_timing.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitor.timing'`

- [ ] **Step 3: Write the implementation**

`lib/monitor/src/monitor/timing.py`:

```python
"""Poll-cadence arithmetic.

A wrong credential retried on the poll cadence is ~1,440 attempts a day. If the
account belongs to a real person, that can cost them access to the thing the
monitor exists to watch. Any authenticated source needs a failure ceiling, not
just backoff — and this module deliberately does not provide one, because only
jeffco has authentication and hoisting its lockout guard here would be a shared
abstraction with exactly one consumer.
"""

from __future__ import annotations

import time
from collections.abc import Callable

# Five minutes. A 30-minute cap would silently undo the poll cadence for half an
# hour after a single transient blip.
MAX_BACKOFF_SEC: float = 300


def system_now() -> int:
    """The wall clock, in epoch seconds.

    One definition, shared by the runner and by every app that needs the time, so
    `main.py` can hand the same callable to both and a test that freezes it freezes
    everything.
    """
    return int(time.time())


def with_jitter(base_sec: float, jitter_pct: float, rand: Callable[[], float]) -> float:
    """Apply +/- jitter to a base interval.

    `jitter_pct` is a percentage (e.g. 20 = +/-20%). `rand()` must return a value
    in [0,1). The result is clamped to >= 1 second.
    """
    offset = base_sec * (jitter_pct / 100) * (rand() * 2 - 1)
    return max(1.0, base_sec + offset)


def next_backoff(current_sec: float, base_sec: float, max_sec: float) -> float:
    """Exponential backoff. Doubles `current_sec`, capped at `max_sec`.

    When `current_sec` is 0 (no prior backoff), starts at `base_sec`.
    """
    nxt = base_sec if current_sec <= 0 else current_sec * 2
    return min(nxt, max_sec)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/lib/test_timing.py -q && uv run mypy --strict lib tests && uv run ruff check .`
Expected: 7 passed, mypy `Success`, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add lib/monitor/src/monitor/timing.py tests/lib/test_timing.py
git commit -m "Add jitter and backoff to the monitor library"
```

---

### Task 3: state.py — the seen-key baseline

4 lines of doc-comment divergence between the two TypeScript apps, so another near-pure translation. The atomicity of the write is the part that matters: a kill mid-write must leave either the old baseline or the new one, never a truncated file that reads as "first run" and silently re-baselines a real channel.

**Files:**
- Create: `lib/monitor/src/monitor/state.py`
- Test: `tests/lib/test_state.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `LoadedState(keys: set[str] | None, corrupt: bool)` frozen dataclass; `load_state(path: str) -> LoadedState`; `save_state(path: str, keys: set[str]) -> None`.

**No injectable filesystem.** An earlier draft had a three-method `StateFs` Protocol with one production implementation, existing so one test could assert a call *sequence*, plus a `state_fs` parameter threaded onto `run_forever`. `monkeypatch` gets stronger coverage without any of it — see Step 1's staging test, which proves the target is untouched mid-write rather than merely proving which functions were called.

**`corrupt` is returned rather than re-derived.** The runner previously called `os.path.exists` itself to tell "missing" from "unparseable", which put filesystem knowledge in two modules and made the distinction untestable through the injected seam. One module owns the file.

- [ ] **Step 1: Write the failing test**

`tests/lib/test_state.py`:

```python
import os
from pathlib import Path

import pytest

from monitor.state import load_state, save_state


def test_missing_file_reads_as_first_run_and_is_not_corrupt(tmp_path: Path) -> None:
    loaded = load_state(str(tmp_path / "missing.json"))
    assert loaded.keys is None
    assert loaded.corrupt is False


def test_round_trips_a_key_set(tmp_path: Path) -> None:
    path = str(tmp_path / "rt.json")
    save_state(path, {"2026-12-01 10:30", "2026-12-02 09:00"})
    loaded = load_state(path)
    assert loaded.keys == {"2026-12-01 10:30", "2026-12-02 09:00"}
    assert loaded.corrupt is False


def test_an_empty_array_is_a_baseline_not_a_first_run(tmp_path: Path) -> None:
    # This is what makes `echo '[]' > state.json` the supported way to ask for the
    # current backlog: it must load as an empty set, never as None.
    path = tmp_path / "empty.json"
    path.write_text("[]", encoding="utf-8")
    assert load_state(str(path)).keys == set()


def test_an_unparseable_file_reads_as_first_run_AND_reports_corrupt(tmp_path: Path) -> None:
    # Missing and corrupt both re-baseline, and they mean opposite things. The flag
    # is what lets the runner say so — the lost keys are unrecoverable, so saying so
    # is the only useful response.
    path = tmp_path / "corrupt.json"
    path.write_text("not json{{{", encoding="utf-8")
    loaded = load_state(str(path))
    assert loaded.keys is None
    assert loaded.corrupt is True


def test_a_json_object_is_not_a_baseline(tmp_path: Path) -> None:
    # Valid JSON of the wrong shape is corruption too: iterating a dict would
    # silently produce a baseline of its keys.
    path = tmp_path / "object.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    loaded = load_state(str(path))
    assert loaded.keys is None
    assert loaded.corrupt is True


def test_the_new_baseline_is_staged_in_a_sibling_before_it_replaces_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Freeze the rename to observe the instant before it. This is stronger than
    # asserting which functions were called: it proves the target still holds the
    # OLD baseline while the new one is on disk, which is exactly the property that
    # makes a kill mid-write survivable.
    path = tmp_path / "atomic.json"
    path.write_text('["old"]', encoding="utf-8")
    # The STRING form, not `monkeypatch.setattr(monitor.state.os, "replace", ...)`:
    # `mypy --strict` implies --no-implicit-reexport, so reaching through the module
    # to the `os` it imported fails with 'does not explicitly export attribute "os"'.
    # Fixing that in the library (`import os as os`) would put a test-driven idiom into
    # production code, where the next tidy-up would silently break this test.
    monkeypatch.setattr("monitor.state.os.replace", lambda src, dst: None)

    save_state(str(path), {"new"})

    assert path.read_text(encoding="utf-8") == '["old"]'
    assert (tmp_path / "atomic.json.tmp").read_text(encoding="utf-8") == '["new"]'


def test_a_completed_save_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "clean.json"
    save_state(str(path), {"a"})
    assert path.read_text(encoding="utf-8") == '["a"]'
    assert not (tmp_path / "clean.json.tmp").exists()


def test_it_creates_the_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "state.json"
    save_state(str(path), {"a"})
    assert load_state(str(path)).keys == {"a"}


def test_the_written_file_is_a_sorted_json_array(tmp_path: Path) -> None:
    path = tmp_path / "sorted.json"
    save_state(str(path), {"c", "a", "b"})
    assert path.read_text(encoding="utf-8") == '["a", "b", "c"]'
    assert os.path.exists(path)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/lib/test_state.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitor.state'`

- [ ] **Step 3: Write the implementation**

`lib/monitor/src/monitor/state.py`:

```python
"""The seen-key baseline: one JSON array of item keys per app.

Sync rather than async on purpose. It is one ~700-byte write per tick against a
10-second interval, so the blocking cost is noise, and keeping it sync means the
baseline has no event-loop dependency and its tests need no async fixture. The
TypeScript version is async only because node's fs API is.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LoadedState:
    """The baseline as found on disk.

    `keys is None` means "no usable baseline, treat this as a first run". `corrupt`
    separates the two ways that happens, because they mean opposite things: missing
    is a normal first boot, while a file that exists and does not parse means the
    baseline is *gone* and this boot will re-baseline silently — the failure that
    looks exactly like everything being fine. Nothing can recover the lost keys, so
    the only useful response is for the runner to say so.
    """

    keys: set[str] | None
    corrupt: bool


def load_state(path: str) -> LoadedState:
    """Load the previous key set, distinguishing missing from unparseable.

    An empty array loads as an empty *set*, not as None — that is what makes
    `echo '[]' > state.json` the supported way to ask for the current backlog.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return LoadedState(keys=None, corrupt=False)
    try:
        parsed = json.loads(raw)
    except ValueError:
        return LoadedState(keys=None, corrupt=True)
    if not isinstance(parsed, list):
        # Valid JSON of the wrong shape is corruption too: iterating a dict would
        # silently produce a baseline of its keys.
        return LoadedState(keys=None, corrupt=True)
    return LoadedState(keys={str(k) for k in parsed}, corrupt=False)


def save_state(path: str, keys: set[str]) -> None:
    """Persist the key set as a JSON array, creating the parent dir if needed.

    Atomic: writes a sibling temp file then renames it over `path`, so a kill
    mid-write cannot corrupt or truncate the baseline. `os.replace`, not
    `os.rename`: replace overwrites an existing destination atomically on every
    platform. A stale `${path}.tmp` from an interrupted rename is harmlessly
    overwritten on the next call (single-writer process), so no cleanup is needed.

    Sorted, unlike the TypeScript version's insertion order: the file is one a
    human diffs when a monitor misbehaves, and a stable ordering makes that diff
    mean something. The content is loaded back into a set, so order is not
    behaviour.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    Path(tmp).write_text(json.dumps(sorted(keys)), encoding="utf-8")
    os.replace(tmp, path)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/lib/test_state.py -q && uv run mypy --strict lib tests && uv run ruff check .`
Expected: 9 passed, mypy `Success`, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add lib/monitor/src/monitor/state.py tests/lib/test_state.py
git commit -m "Add the atomic seen-key baseline to the monitor library"
```

---

### Task 4: health.py — liveness state and the configured heartbeat hour

Jeffco's 4 exports are a strict subset of melanzana's 8; the health *server* is not ported at all (production reports `health=off`, making it dead code). The new work here is `HEARTBEAT_AT`, the only behavioural addition the library makes on its own initiative.

**Files:**
- Create: `lib/monitor/src/monitor/health.py`
- Test: `tests/lib/test_health.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `HealthState` frozen dataclass with `started_unix: int`, `last_success_unix: int`, `consecutive_failures: int`, `items_tracked: int`, `last_heartbeat_unix: int`, `death_alerted: bool`; `init_health(started_unix: int) -> HealthState`; `is_stalled(state: HealthState, now: int, stall_sec: int) -> bool`; `should_heartbeat(state: HealthState, now: int, interval_sec: int, heartbeat_at: tuple[int, int] | None = None) -> bool`; `OPERATOR_TZ: ZoneInfo`.

Note the field rename: melanzana's `slotsTracked` and jeffco's `jobsTracked` become one `items_tracked`, which is the whole reason `OpsLabels.tracked_noun` exists — the count is generic, the noun is not.

- [ ] **Step 1: Write the failing test**

`tests/lib/test_health.py`. The Denver epochs are computed, not guessed — December is MST (UTC−7):

```python
from dataclasses import replace

from monitor.health import init_health, is_stalled, should_heartbeat

# America/Denver wall-clock instants, MST (UTC-7) in December.
DEC1_0600 = 1796130000
DEC1_0700 = 1796133600
DEC1_0800 = 1796137200
DEC1_2300 = 1796191200
DEC2_0659 = 1796219940
DEC2_0700 = 1796220000
DEC2_0701 = 1796220060
AT_0700 = (7, 0)


def test_init_seeds_both_clocks_so_neither_fires_at_boot() -> None:
    h = init_health(1000)
    assert h.last_success_unix == 1000
    assert h.last_heartbeat_unix == 1000
    assert h.consecutive_failures == 0
    assert h.items_tracked == 0
    assert h.death_alerted is False
    assert is_stalled(h, 1000, 600) is False
    assert should_heartbeat(h, 1000, 86400) is False


def test_is_stalled_is_false_just_under_the_threshold() -> None:
    assert is_stalled(init_health(1000), 1599, 600) is False


def test_is_stalled_is_true_at_exactly_the_threshold() -> None:
    # >= : the boundary instant itself counts as stalled.
    assert is_stalled(init_health(1000), 1600, 600) is True


def test_heartbeat_is_not_due_before_the_interval_elapses() -> None:
    assert should_heartbeat(init_health(1000), 1000 + 86399, 86400) is False


def test_heartbeat_is_due_once_the_interval_elapses() -> None:
    assert should_heartbeat(init_health(1000), 1000 + 86400, 86400) is True


# --- HEARTBEAT_AT ---------------------------------------------------------


def test_at_hour_is_not_due_later_the_same_local_day() -> None:
    # The date check is what stops it firing repeatedly for the rest of the day —
    # note the interval has long since elapsed here and it still must not fire.
    h = replace(init_health(DEC1_0700), last_heartbeat_unix=DEC1_0700)
    assert should_heartbeat(h, DEC1_2300, 86400, AT_0700) is False


def test_at_hour_is_not_due_on_a_new_local_day_before_the_hour() -> None:
    h = replace(init_health(DEC1_0700), last_heartbeat_unix=DEC1_0700)
    assert should_heartbeat(h, DEC2_0659, 86400, AT_0700) is False


def test_at_hour_is_due_at_exactly_the_hour_on_a_new_local_day() -> None:
    h = replace(init_health(DEC1_0700), last_heartbeat_unix=DEC1_0700)
    assert should_heartbeat(h, DEC2_0700, 86400, AT_0700) is True


def test_at_hour_is_due_past_the_hour_on_a_new_local_day() -> None:
    h = replace(init_health(DEC1_0700), last_heartbeat_unix=DEC1_0700)
    assert should_heartbeat(h, DEC2_0701, 86400, AT_0700) is True


def test_at_hour_ignores_the_interval_entirely() -> None:
    # A one-second interval must not drag the heartbeat off its hour.
    h = replace(init_health(DEC1_0700), last_heartbeat_unix=DEC1_0700)
    assert should_heartbeat(h, DEC1_0800, 1, AT_0700) is False


def test_a_process_starting_an_hour_early_waits_until_the_next_day() -> None:
    # The documented worst case: seeded lastHeartbeat is already today, so the
    # first heartbeat lands ~25 hours later. That is the intended trade for a
    # predictable hour.
    h = init_health(DEC1_0600)
    assert should_heartbeat(h, DEC1_0700, 86400, AT_0700) is False
    assert should_heartbeat(h, DEC2_0700, 86400, AT_0700) is True
    assert (DEC2_0700 - DEC1_0600) / 3600 == 25


def test_a_backwards_clock_step_does_not_fire_a_heartbeat() -> None:
    # "The local date has changed" means advanced. An NTP correction that steps
    # the clock back a day must not be read as a new day.
    h = replace(init_health(DEC2_0700), last_heartbeat_unix=DEC2_0700)
    assert should_heartbeat(h, DEC1_0800, 86400, AT_0700) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/lib/test_health.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitor.health'`

- [ ] **Step 3: Write the implementation**

`lib/monitor/src/monitor/health.py`:

```python
"""Liveness bookkeeping: the state the loop folds each tick, and the predicates
that decide when the monitor should speak about itself.

The health *server* is deliberately not here. Production runs with `health=off`,
which made `startHealthServer`, `toSnapshot`, and `HealthSnapshot` dead code in
the TypeScript app along with the assertions covering them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

# Pinned rather than configurable, for the same reason jeffco pins it in code:
# making the zone an env var only creates a way to typo an identifier and raise on
# every tick forever. It is the operator's clock, not the watched site's, so it is
# right even for a source in another country.
OPERATOR_TZ = ZoneInfo("America/Denver")


@dataclass(frozen=True)
class HealthState:
    """In-memory liveness snapshot, folded once per tick by the loop.

    `items_tracked` is the generic name for what the TypeScript apps called
    `slotsTracked` and `jobsTracked`. The count is shared; the noun it is read
    with is not, which is why `OpsLabels.tracked_noun` exists.
    """

    started_unix: int
    last_success_unix: int
    consecutive_failures: int
    items_tracked: int
    last_heartbeat_unix: int
    death_alerted: bool


def init_health(started_unix: int) -> HealthState:
    """Fresh state at process start.

    Seeds `last_success_unix` AND `last_heartbeat_unix` to `started_unix`, so boot
    neither looks stalled nor emits an immediate heartbeat.
    """
    return HealthState(
        started_unix=started_unix,
        last_success_unix=started_unix,
        consecutive_failures=0,
        items_tracked=0,
        last_heartbeat_unix=started_unix,
        death_alerted=False,
    )


def is_stalled(state: HealthState, now: int, stall_sec: int) -> bool:
    """True when no successful poll has landed for at least `stall_sec`."""
    # >= : the boundary instant itself counts as stalled.
    return now - state.last_success_unix >= stall_sec


def should_heartbeat(
    state: HealthState,
    now: int,
    interval_sec: int,
    heartbeat_at: tuple[int, int] | None = None,
) -> bool:
    """True when a heartbeat is due.

    **`heartbeat_at` unset** — `interval_sec` since the last heartbeat. This is
    what melanzana does today, and what keeps its port at parity by default.

    **`heartbeat_at` set**, as `(hour, minute)` in `OPERATOR_TZ` — due when the
    local date has advanced since the last heartbeat *and* the local time is at or
    past it. Both conditions are needed: the date check is what stops it firing
    repeatedly for the rest of the day.

    Why the hour exists at all: `interval_sec` alone anchors the heartbeat to
    process start, so the hour it arrives is whatever time the last deploy
    happened, it re-anchors on every restart, and it creeps later by up to one poll
    interval a day. An ops message that turns up at 01:05 because that is when a
    migration finished is not useful.
    """
    if heartbeat_at is None:
        # >= : the boundary instant itself counts as due.
        return now - state.last_heartbeat_unix >= interval_sec

    local_now = datetime.fromtimestamp(now, OPERATOR_TZ)
    local_last = datetime.fromtimestamp(state.last_heartbeat_unix, OPERATOR_TZ)
    # <= rather than == : "the date changed" means advanced. A clock stepped
    # backwards by an NTP correction is not a new day.
    if local_now.date() <= local_last.date():
        return False
    return (local_now.hour, local_now.minute) >= heartbeat_at
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/lib/test_health.py -q && uv run mypy --strict lib tests && uv run ruff check .`
Expected: 12 passed, mypy `Success`, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add lib/monitor/src/monitor/health.py tests/lib/test_health.py
git commit -m "Add liveness state and the configured heartbeat hour"
```

---

### Task 5: config.py — env primitives and the runner's schema

The measured divergence was "helpers shareable; the schema is per-app", so the helpers live here and each app assembles its own schema from them. `load_runner_config` reads only the names the *library* acts on.

Two deliberate wording changes from melanzana, both covered by the spec's "log line wording is not parity-covered": the URL check adopts jeffco's stronger validation, and `HEALTH_PORT` is gone with the health server.

**Files:**
- Create: `lib/monitor/src/monitor/config.py`
- Test: `tests/lib/test_config.py`

**Interfaces:**
- Consumes: `monitor.types.OpsLabels`, `monitor.timing.MAX_BACKOFF_SEC`.
- Produces: `Env = Mapping[str, str | None]`; `ConfigError(ValueError)`; `env_num(env, key, fallback: float, minimum: float = 1) -> float`; `env_str(env, key, fallback: str) -> str`; `env_bool(env, key, fallback: bool) -> bool`; `env_required(env, key) -> str`; `env_https_url(key: str, raw: str) -> str`; `env_time(env, key) -> tuple[int, int] | None`; `make_log(prefix: str) -> Callable[[str], None]`; `RunnerConfig` frozen dataclass; `load_runner_config(env, *, labels, log_prefix, default_poll_interval_sec, default_state_path="/data/state.json") -> RunnerConfig`.

`RunnerConfig` fields: `alert_webhook_url: str`, `status_webhook_url: str | None`, `state_path: str`, `poll_interval_sec: float`, `poll_jitter_pct: float`, `heartbeat_interval_sec: int`, `heartbeat_at: tuple[int, int] | None`, `stall_alert_sec: int`, `labels: OpsLabels`, `log_prefix: str`, `max_backoff_sec: float = MAX_BACKOFF_SEC`, `post_spacing_sec: float = 0.35`. Plus one property, `status_url: str`.

Two things moved here from elsewhere:

- **`status_url` is a `RunnerConfig` property**, not a `resolve_status_url(cfg)` function in `discord.py`. It was the only reason a formatter-and-transport module imported the runner's config schema, and the fallback is a fact about the config.
- **`make_log(prefix)`** so the log prefix is written once. Otherwise every app repeats its own name in two files — `log_prefix="melanzana-monitor"` in its config and `print(f"[melanzana-monitor] …")` in its `main.py` — and a Monitor implementation that wants to log (jeffco logs each newly-seen filter gap once) has nothing to reach for.

- [ ] **Step 1: Write the failing test**

`tests/lib/test_config.py`:

```python
import pytest

from monitor.config import (
    ConfigError,
    env_bool,
    env_https_url,
    env_num,
    env_required,
    env_str,
    env_time,
    load_runner_config,
    make_log,
)
from monitor.types import OpsLabels

LABELS = OpsLabels(name="X monitor", tracked_noun="thing(s)", death_footer="footer.")
WEBHOOK = "https://discord.test/webhook"


def test_env_num_falls_back_when_unset_or_empty() -> None:
    assert env_num({}, "N", 10) == 10
    assert env_num({"N": ""}, "N", 10) == 10


def test_env_num_parses_a_value() -> None:
    assert env_num({"N": "30"}, "N", 10) == 30


def test_env_num_rejects_a_non_number() -> None:
    with pytest.raises(ConfigError, match="N must be a number"):
        env_num({"N": "abc"}, "N", 10)


def test_env_num_rejects_a_non_finite_number() -> None:
    # float("inf") parses where JS Number("Infinity") also parses; both must be
    # rejected, or POLL_INTERVAL_SEC=inf becomes a monitor that never polls again.
    with pytest.raises(ConfigError, match="N must be a number"):
        env_num({"N": "inf"}, "N", 10)


def test_env_num_rejects_a_value_below_its_minimum() -> None:
    with pytest.raises(ConfigError, match="N must be >= 1"):
        env_num({"N": "0"}, "N", 60)


def test_env_num_allows_zero_when_the_minimum_is_zero() -> None:
    assert env_num({"N": "0"}, "N", 20, minimum=0) == 0


def test_env_str_and_env_bool() -> None:
    assert env_str({}, "S", "fallback") == "fallback"
    assert env_str({"S": ""}, "S", "fallback") == "fallback"
    assert env_str({"S": "given"}, "S", "fallback") == "given"
    assert env_bool({}, "B", False) is False
    assert env_bool({"B": "true"}, "B", False) is True
    assert env_bool({"B": "TRUE"}, "B", False) is True
    assert env_bool({"B": "yes"}, "B", False) is False


def test_env_required_rejects_missing_and_empty() -> None:
    with pytest.raises(ConfigError, match="R is required"):
        env_required({}, "R")
    with pytest.raises(ConfigError, match="R is required"):
        env_required({"R": ""}, "R")
    assert env_required({"R": "v"}, "R") == "v"


def test_env_https_url_accepts_an_https_url() -> None:
    assert env_https_url("W", WEBHOOK) == WEBHOOK


def test_env_https_url_rejects_a_non_url_and_a_truncated_paste() -> None:
    # `https://` alone passes a startsWith check and then fails on the first POST,
    # hours later, in a place with no useful context.
    for raw in ("oops", "https://", "http://discord.test/webhook"):
        with pytest.raises(ConfigError, match="W must be an https:// URL"):
            env_https_url("W", raw)


def test_env_https_url_never_echoes_the_value() -> None:
    # A webhook's path IS its credential, and config errors get logged. Use a
    # rejected (non-https) URL so a raise actually happens, then check the
    # message: an accepted URL never reaches an error message at all.
    with pytest.raises(ConfigError) as exc:
        env_https_url("W", "http://discord.test/leaked-secret-path")
    assert "leaked-secret-path" not in str(exc.value)


def test_env_time_is_none_when_unset() -> None:
    assert env_time({}, "HEARTBEAT_AT") is None
    assert env_time({"HEARTBEAT_AT": ""}, "HEARTBEAT_AT") is None


def test_env_time_parses_hh_mm() -> None:
    assert env_time({"HEARTBEAT_AT": "07:00"}, "HEARTBEAT_AT") == (7, 0)
    assert env_time({"HEARTBEAT_AT": "00:00"}, "HEARTBEAT_AT") == (0, 0)
    assert env_time({"HEARTBEAT_AT": "23:59"}, "HEARTBEAT_AT") == (23, 59)


def test_env_time_raises_rather_than_ignoring_a_malformed_value() -> None:
    # Silently falling back to the interval would leave the operator believing a
    # 07:00 report is configured when it is not.
    for raw in ("7:00", "24:00", "07:60", "0700", "morning"):
        with pytest.raises(ConfigError, match="HEARTBEAT_AT must be an HH:MM"):
            env_time({"HEARTBEAT_AT": raw}, "HEARTBEAT_AT")


def test_make_log_stamps_every_line_with_the_app_name(capsys: pytest.CaptureFixture[str]) -> None:
    make_log("melanzana-monitor")("started")
    assert capsys.readouterr().out == "[melanzana-monitor] started\n"


def test_load_runner_config_applies_defaults() -> None:
    cfg = load_runner_config(
        {"DISCORD_WEBHOOK_URL": WEBHOOK},
        labels=LABELS,
        log_prefix="x-monitor",
        default_poll_interval_sec=10,
    )
    assert cfg.alert_webhook_url == WEBHOOK
    assert cfg.status_webhook_url is None
    assert cfg.state_path == "/data/state.json"
    assert cfg.poll_interval_sec == 10
    assert cfg.poll_jitter_pct == 20
    assert cfg.heartbeat_interval_sec == 86400
    assert cfg.heartbeat_at is None
    assert cfg.stall_alert_sec == 600
    assert cfg.max_backoff_sec == 300
    assert cfg.post_spacing_sec == 0.35
    assert cfg.labels is LABELS
    assert cfg.log_prefix == "x-monitor"


def test_load_runner_config_reads_every_shared_name() -> None:
    cfg = load_runner_config(
        {
            "DISCORD_WEBHOOK_URL": WEBHOOK,
            "STATUS_WEBHOOK_URL": "https://discord.test/ops",
            "STATE_PATH": "/tmp/state.json",
            "POLL_INTERVAL_SEC": "30",
            "POLL_JITTER_PCT": "0",
            "HEARTBEAT_INTERVAL_SEC": "3600",
            "HEARTBEAT_AT": "07:00",
            "STALL_ALERT_SEC": "120",
        },
        labels=LABELS,
        log_prefix="x-monitor",
        default_poll_interval_sec=10,
    )
    assert cfg.status_webhook_url == "https://discord.test/ops"
    assert cfg.state_path == "/tmp/state.json"
    assert cfg.poll_interval_sec == 30
    assert cfg.poll_jitter_pct == 0
    assert cfg.heartbeat_interval_sec == 3600
    assert cfg.heartbeat_at == (7, 0)
    assert cfg.stall_alert_sec == 120


def test_load_runner_config_requires_an_https_alert_webhook() -> None:
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL is required"):
        load_runner_config({}, labels=LABELS, log_prefix="x", default_poll_interval_sec=10)
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL must be an https"):
        load_runner_config(
            {"DISCORD_WEBHOOK_URL": "oops"},
            labels=LABELS,
            log_prefix="x",
            default_poll_interval_sec=10,
        )


def test_load_runner_config_validates_the_status_webhook_too() -> None:
    with pytest.raises(ConfigError, match="STATUS_WEBHOOK_URL must be an https"):
        load_runner_config(
            {"DISCORD_WEBHOOK_URL": WEBHOOK, "STATUS_WEBHOOK_URL": "oops"},
            labels=LABELS,
            log_prefix="x",
            default_poll_interval_sec=10,
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/lib/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitor.config'`

- [ ] **Step 3: Write the implementation**

`lib/monitor/src/monitor/config.py`:

```python
"""Environment primitives, and the schema the *runner* acts on.

Every app's own schema is its own — melanzana reads CALENDAR_ID, jeffco reads
SFE_PIN, and neither belongs here. What is shared is the parsing, the validation,
and the handful of names the loop itself consumes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from monitor.timing import MAX_BACKOFF_SEC
from monitor.types import OpsLabels

Env = Mapping[str, str | None]


class ConfigError(ValueError):
    """A configuration value is missing or unusable. Raised at startup, never later."""


def _raw(env: Env, key: str) -> str | None:
    """The value, treating empty string as unset — an env file line `FOO=` is a
    variable someone commented out by deleting its value, not a value of ""."""
    value = env.get(key)
    return None if value is None or value == "" else value


def env_num(env: Env, key: str, fallback: float, minimum: float = 1) -> float:
    raw = _raw(env, key)
    if raw is None:
        return fallback
    try:
        parsed = float(raw)
    except ValueError:
        raise ConfigError(f'Config error: {key} must be a number, got "{raw}"') from None
    # isfinite: float("inf") and float("nan") both parse. POLL_INTERVAL_SEC=inf is
    # a monitor that never polls again, and nan compares false against every bound.
    if not math.isfinite(parsed):
        raise ConfigError(f'Config error: {key} must be a number, got "{raw}"')
    if parsed < minimum:
        raise ConfigError(f'Config error: {key} must be >= {minimum}, got "{raw}"')
    return parsed


def env_str(env: Env, key: str, fallback: str) -> str:
    raw = _raw(env, key)
    return fallback if raw is None else raw


def env_bool(env: Env, key: str, fallback: bool) -> bool:
    """Only the exact string "true" (any case) is true.

    Deliberately not a list of truthy spellings: MENTION_EVERYONE decides whether a
    channel of people gets pinged, and "yes" quietly meaning false is a smaller
    failure than a permissive parser meaning true by accident.
    """
    raw = _raw(env, key)
    return fallback if raw is None else raw.lower() == "true"


def env_required(env: Env, key: str) -> str:
    raw = _raw(env, key)
    if raw is None:
        raise ConfigError(f"Config error: {key} is required")
    return raw


def env_https_url(key: str, raw: str) -> str:
    """Validate a webhook URL. Two rules, both load-bearing.

    1. Parsed, not prefix-matched. A truncated paste like `https://` passes a
       `startswith` check and then fails on the first POST — hours later, in a
       place with no useful context.
    2. The message never contains `raw`. A Discord webhook's path IS its
       credential, and config errors get logged.
    """
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigError(f"Config error: {key} must be an https:// URL")
    return raw


_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def env_time(env: Env, key: str) -> tuple[int, int] | None:
    """Parse an `HH:MM` 24-hour local time. None when unset.

    Raises rather than ignoring a malformed value: this variable exists to make the
    heartbeat hour predictable, so silently falling back to the interval would
    leave the operator believing a 07:00 report is configured when it is not. An
    HH:MM is not a credential, so echoing it is safe and useful.
    """
    raw = _raw(env, key)
    if raw is None:
        return None
    match = _HHMM.match(raw)
    if match is None:
        raise ConfigError(
            f'Config error: {key} must be an HH:MM 24-hour local time, got "{raw}"'
        )
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True)
class RunnerConfig:
    """Everything `run_forever` reads. An app holds this plus its own fields."""

    alert_webhook_url: str
    status_webhook_url: str | None
    state_path: str
    poll_interval_sec: float
    poll_jitter_pct: float
    heartbeat_interval_sec: int
    heartbeat_at: tuple[int, int] | None
    stall_alert_sec: int
    labels: OpsLabels
    log_prefix: str
    max_backoff_sec: float = MAX_BACKOFF_SEC
    # Gap between the messages of one batch. Discord allows roughly five requests
    # per two seconds per webhook — so 0.35s leaves almost no margin, and a 429 is
    # handled explicitly in run_tick rather than merely made unlikely here. A code
    # constant, not an env var: it needs a reason attached, not a knob.
    post_spacing_sec: float = 0.35

    @property
    def status_url(self) -> str:
        """Where ops messages go: the separate status channel when one is
        configured, else the main alert channel. Both are credentials — the point of
        naming this is that neither is ever logged."""
        return self.status_webhook_url or self.alert_webhook_url


def make_log(prefix: str) -> Callable[[str], None]:
    """A logger that stamps every line with the app's name.

    `flush=True` because the container's stdout is what `docker logs` and the Cloud
    Logging agent read, and a crashed process must not lose its last words to a
    buffer.
    """

    def log(message: str) -> None:
        print(f"[{prefix}] {message}", flush=True)

    return log


def load_runner_config(
    env: Env,
    *,
    labels: OpsLabels,
    log_prefix: str,
    default_poll_interval_sec: float,
    default_state_path: str = "/data/state.json",
) -> RunnerConfig:
    """Read the shared names. The poll interval's default is per-app: melanzana
    polls every 10s, jeffco every 60s because it shares a login with a person."""
    status_raw = _raw(env, "STATUS_WEBHOOK_URL")
    return RunnerConfig(
        alert_webhook_url=env_https_url(
            "DISCORD_WEBHOOK_URL", env_required(env, "DISCORD_WEBHOOK_URL")
        ),
        status_webhook_url=(
            None if status_raw is None else env_https_url("STATUS_WEBHOOK_URL", status_raw)
        ),
        state_path=env_str(env, "STATE_PATH", default_state_path),
        poll_interval_sec=env_num(env, "POLL_INTERVAL_SEC", default_poll_interval_sec),
        poll_jitter_pct=env_num(env, "POLL_JITTER_PCT", 20, minimum=0),
        heartbeat_interval_sec=int(env_num(env, "HEARTBEAT_INTERVAL_SEC", 86400)),
        heartbeat_at=env_time(env, "HEARTBEAT_AT"),
        stall_alert_sec=int(env_num(env, "STALL_ALERT_SEC", 600)),
        labels=labels,
        log_prefix=log_prefix,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/lib/test_config.py -q && uv run mypy --strict lib tests && uv run ruff check .`
Expected: 19 passed, mypy `Success`, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add lib/monitor/src/monitor/config.py tests/lib/test_config.py
git commit -m "Add env primitives and the runner config schema"
```

---

### Task 6: discord.py — transport and ops messages

`postAlert` was shared verbatim between the apps; alert formatting was genuinely per-app and stays in the app. The ops messages are shared *shapes* with three per-app strings, which is what `OpsLabels` carries.

**Files:**
- Create: `lib/monitor/src/monitor/discord.py`
- Test: `tests/lib/test_discord.py`

**Interfaces:**
- Consumes: `monitor.types` (`BLUE`, `Embed`, `Field`, `GREEN`, `HeartbeatExtras`, `OpsLabels`, `Payload`, `RED`), `monitor.health.HealthState`. **Not `monitor.config`** and **not `httpx`.**
- Produces: `DiscordPostError(Exception)` carrying `status_code: int | None`; `HttpResponse`/`HttpClient` Protocols; `Poster = Callable[[str, Payload], Awaitable[None]]`; `StatusPoster = Callable[[Payload], Awaitable[None]]`; `RETRYABLE_STATUS: frozenset[int]`; `async post(url: str, payload: Payload, client: HttpClient) -> None`; `format_heartbeat(labels, state, now_unix, extras: HeartbeatExtras = HeartbeatExtras()) -> Payload`; `format_status_alert(kind: Literal["death", "recovery"], labels, state, now_unix) -> Payload`; `format_delivery_failure(labels, status_code: int, item_count: int) -> Payload`.

Three changes from the first draft, all of them corrections:

- **`client: HttpClient`, a structural Protocol, not `httpx.AsyncClient`.** The spec's stated reason for choosing Python at all is that fashionjobs needs `curl_cffi` with `impersonate="chrome"` to clear Cloudflare's TLS fingerprinting. Under `mypy --strict` a `curl_cffi` session does not satisfy an `httpx.AsyncClient` annotation, so app #3 would have to reimplement `post` — duplicating the non-2xx check *and* the "never put the webhook URL in the error message" rule, which is the last invariant that should ever be copy-pasted. Four lines here means app #3 needs no library edit.
- **`DiscordPostError.status_code`**, because `run_tick` now distinguishes retryable from permanent. An exception that drops the status is the mistake jeffco's `SfeHttpError` was written to avoid.
- **`format_delivery_failure`**, the ops message for a permanent rejection. It belongs here with the other ops messages.

- [ ] **Step 1: Write the failing test**

`tests/lib/test_discord.py`:

```python
from dataclasses import replace

import httpx
import pytest

from monitor.config import load_runner_config
from monitor.discord import (
    DiscordPostError,
    format_delivery_failure,
    format_heartbeat,
    format_status_alert,
    post,
)
from monitor.health import init_health
from monitor.types import BLUE, GREEN, RED, Embed, Field, HeartbeatExtras, OpsLabels, Payload

LABELS = OpsLabels(
    name="Melanzana monitor",
    tracked_noun="slot(s)",
    death_footer="Liveness alert — the monitor may be blocked or down.",
)
WEBHOOK = "https://discord.test/webhook"


def _payload() -> Payload:
    return Payload(embeds=(Embed(title="t", description="d", color=GREEN),))


async def test_post_sends_the_payload_as_json() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await post(WEBHOOK, _payload(), client)

    assert seen["url"] == WEBHOOK
    assert '"title": "t"' in str(seen["body"]) or '"title":"t"' in str(seen["body"])


async def test_post_raises_on_a_non_2xx_response_and_keeps_the_status() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(429))
    ) as client:
        with pytest.raises(DiscordPostError, match="429") as exc:
            await post(WEBHOOK, _payload(), client)
    assert exc.value.status_code == 429


def test_retryable_covers_rate_limits_5xx_and_unknown_but_not_a_bad_request() -> None:
    # The whole point of carrying the status: a 429 will succeed later, a 400 never
    # will, and a network error with no status gets the cautious reading.
    assert DiscordPostError("x", 429).retryable is True
    assert DiscordPostError("x", 503).retryable is True
    assert DiscordPostError("x", None).retryable is True
    assert DiscordPostError("x", 400).retryable is False
    assert DiscordPostError("x", 404).retryable is False


async def test_post_accepts_any_client_with_the_right_shape() -> None:
    # Proves the transport is structural: this stands in for curl_cffi, which
    # fashionjobs needs and which is not an httpx.AsyncClient.
    seen: dict[str, object] = {}

    class FakeResponse:
        status_code = 204

    class NotHttpx:
        async def post(self, url: str, *, json: object, headers: object) -> FakeResponse:
            seen["url"] = url
            seen["json"] = json
            return FakeResponse()

    await post(WEBHOOK, _payload(), NotHttpx())
    assert seen["url"] == WEBHOOK
    assert seen["json"] == _payload().to_dict()


async def test_post_error_never_contains_the_webhook_url() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    ) as client:
        with pytest.raises(DiscordPostError) as exc:
            await post("https://discord.test/secret-path", _payload(), client)
    assert "secret-path" not in str(exc.value)


def test_heartbeat_matches_melanzanas_wording_and_never_pings() -> None:
    state = replace(init_health(1000), items_tracked=7, last_success_unix=1500)
    payload = format_heartbeat(LABELS, state, 1000 + 3600)
    assert payload.content is None
    assert payload.allowed_mentions_parse is None
    embed = payload.embeds[0]
    assert embed.title == "💚 Melanzana monitor — still watching"
    assert embed.description == (
        "Up 1h · tracking 7 slot(s) · last successful poll <t:1500:R>."
    )
    assert embed.color == BLUE
    assert embed.footer_text == "Routine heartbeat — no action needed."
    # No fields key at all when the app has nothing to add — matches the
    # TypeScript embed, which never sets one.
    assert embed.fields is None
    assert "fields" not in payload.to_dict()["embeds"][0]


def test_heartbeat_carries_the_apps_extra_fields() -> None:
    # Jeffco's heartbeat reports the school names its filter did not recognise.
    # The library decides *when*; the app supplies the content.
    gap = Field(name="Schools not on the list", value="• X", inline=False)
    payload = format_heartbeat(LABELS, init_health(1000), 1000, HeartbeatExtras(fields=(gap,)))
    assert payload.embeds[0].fields == (gap,)
    # A field without a footer change keeps the default wording.
    assert payload.embeds[0].footer_text == "Routine heartbeat — no action needed."


def test_an_app_can_replace_the_heartbeat_footer() -> None:
    # This is the case a list[Field] return could not express: jeffco swaps the
    # footer for the instruction that makes the gap report actionable.
    payload = format_heartbeat(
        LABELS,
        init_health(1000),
        1000,
        HeartbeatExtras(
            fields=(Field(name="Schools not on the list", value="• X", inline=False),),
            footer_text="If any of these are high schools, add them to HS_SCHOOLS.",
        ),
    )
    assert payload.embeds[0].footer_text == (
        "If any of these are high schools, add them to HS_SCHOOLS."
    )


def test_a_delivery_failure_alert_says_the_items_were_abandoned_and_never_pings() -> None:
    payload = format_delivery_failure(LABELS, 400, 3)
    assert payload.content is None
    assert payload.allowed_mentions_parse is None
    embed = payload.embeds[0]
    assert "HTTP 400" in embed.description
    assert "3 slot(s)" in embed.description
    # The reader has to understand that these items are gone, not delayed.
    assert "will NOT be announced" in embed.description
    assert embed.color == RED


def test_death_alert_matches_melanzanas_wording_and_never_pings() -> None:
    state = replace(init_health(1000), last_success_unix=1000, consecutive_failures=4)
    payload = format_status_alert("death", LABELS, state, 2000)
    assert payload.content is None
    assert payload.allowed_mentions_parse is None
    embed = payload.embeds[0]
    assert embed.title == "⚠️ Melanzana monitor — no successful poll"
    assert embed.description == (
        "No successful poll since <t:1000:R> (4 consecutive failures). "
        "Still retrying; you'll get one more message when it recovers."
    )
    assert embed.color == RED
    assert embed.footer_text == "Liveness alert — the monitor may be blocked or down."


def test_recovery_alert_matches_melanzanas_wording_and_never_pings() -> None:
    payload = format_status_alert("recovery", LABELS, init_health(1000), 2100)
    assert payload.content is None
    assert payload.allowed_mentions_parse is None
    embed = payload.embeds[0]
    assert embed.title == "✅ Melanzana monitor — recovered"
    assert embed.description == "Polling succeeded again <t:2100:R>. Back to normal."
    assert embed.color == GREEN
    assert embed.footer_text == "Liveness alert."


def test_status_url_falls_back_to_the_alert_webhook() -> None:
    base = {"DISCORD_WEBHOOK_URL": WEBHOOK}
    cfg = load_runner_config(base, labels=LABELS, log_prefix="x", default_poll_interval_sec=10)
    assert cfg.status_url == WEBHOOK

    separate = load_runner_config(
        {**base, "STATUS_WEBHOOK_URL": "https://discord.test/ops"},
        labels=LABELS,
        log_prefix="x",
        default_poll_interval_sec=10,
    )
    assert separate.status_url == "https://discord.test/ops"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/lib/test_discord.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitor.discord'`

- [ ] **Step 3: Write the implementation**

`lib/monitor/src/monitor/discord.py`:

```python
"""Webhook transport, and the ops messages every app sends about itself.

Alert formatting is deliberately absent: melanzana renders one embed with day-card
fields, jeffco renders one message per job, and there is no shared shape between
them worth naming. Heartbeats and liveness alerts *are* shared, down to three
strings — see OpsLabels.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol

from monitor.health import HealthState
from monitor.types import BLUE, GREEN, RED, Embed, HeartbeatExtras, OpsLabels, Payload

#: Posts one payload to one webhook. The runner takes this rather than a client so
#: tests can drive failure without a transport, and so an app can wrap it.
Poster = Callable[[str, Payload], Awaitable[None]]

#: Posts an ops message. Its implementation must swallow its own errors.
StatusPoster = Callable[[Payload], Awaitable[None]]

#: Worth trying again. Everything else in 4xx is the request's own fault and will be
#: rejected identically forever, so retrying it is a way of never noticing.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpResponse(Protocol):
    """Just the status. Both httpx and curl_cffi responses satisfy this."""

    @property
    def status_code(self) -> int: ...


class HttpClient(Protocol):
    """The transport surface `post` needs, structurally.

    Deliberately NOT `httpx.AsyncClient`. fashionjobs must use `curl_cffi` with
    `impersonate="chrome"` — that is the reason this project is Python at all — and
    naming a concrete client here would force it to reimplement this function,
    security invariant included.
    """

    async def post(
        self, url: str, *, json: Any, headers: Mapping[str, str]
    ) -> HttpResponse: ...


class DiscordPostError(Exception):
    """A webhook POST was rejected.

    Never carries the URL — it is a credential, and this text reaches the logs. It
    does carry the status, because the caller's response depends on it: a 429 is
    worth retrying and a 400 never will be.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        """Unknown (a network error, no status at all) counts as retryable: the
        cautious reading is that the message may yet be delivered."""
        return self.status_code is None or self.status_code in RETRYABLE_STATUS


async def post(url: str, payload: Payload, client: HttpClient) -> None:
    """POST a payload to a Discord webhook. Raises on a non-2xx response."""
    response = await client.post(
        url, json=payload.to_dict(), headers={"content-type": "application/json"}
    )
    if not 200 <= response.status_code < 300:
        raise DiscordPostError(
            f"Discord webhook failed: HTTP {response.status_code}", response.status_code
        )


def format_heartbeat(
    labels: OpsLabels,
    state: HealthState,
    now_unix: int,
    extras: HeartbeatExtras = HeartbeatExtras(),
) -> Payload:
    """Heartbeat ops message — confirms the monitor is alive. Never pings.

    `<t:UNIX:R>` renders as Discord-native relative time ("2 hours ago").

    `extras.footer_text` wins over the default when the app sets one: jeffco's
    heartbeat lists the schools its filter did not recognise and replaces the footer
    with what to do about them, which is the actionable half of that report.
    """
    uptime_hours = (now_unix - state.started_unix) // 3600
    return Payload(
        embeds=(
            Embed(
                title=f"💚 {labels.name} — still watching",
                description=(
                    f"Up {uptime_hours}h · tracking {state.items_tracked} "
                    f"{labels.tracked_noun} · last successful poll "
                    f"<t:{state.last_success_unix}:R>."
                ),
                color=BLUE,
                # None, not (), when the app adds nothing: an empty tuple would
                # emit `"fields": []`, which the TypeScript embed never does.
                fields=extras.fields or None,
                footer_text=extras.footer_text or "Routine heartbeat — no action needed.",
            ),
        )
    )


def format_status_alert(
    kind: Literal["death", "recovery"],
    labels: OpsLabels,
    state: HealthState,
    now_unix: int,
) -> Payload:
    """Liveness ops message. `death` = sustained inability to poll; `recovery` =
    polling resumed. Never pings — only real item alerts do."""
    if kind == "death":
        return Payload(
            embeds=(
                Embed(
                    title=f"⚠️ {labels.name} — no successful poll",
                    description=(
                        f"No successful poll since <t:{state.last_success_unix}:R> "
                        f"({state.consecutive_failures} consecutive failures). "
                        f"Still retrying; you'll get one more message when it recovers."
                    ),
                    color=RED,
                    footer_text=labels.death_footer,
                ),
            )
        )
    return Payload(
        embeds=(
            Embed(
                title=f"✅ {labels.name} — recovered",
                description=f"Polling succeeded again <t:{now_unix}:R>. Back to normal.",
                color=GREEN,
                footer_text="Liveness alert.",
            ),
        )
    )


def format_delivery_failure(labels: OpsLabels, status_code: int, item_count: int) -> Payload:
    """A permanently rejected alert. Never pings.

    Withholding a key assumes the next tick can succeed. When Discord rejects a
    payload for what it *is* — over the 6000-character total embed cap, most
    likely — every retry is rejected identically, so the runner banks the keys to
    stop the loop and posts this instead. Without it that path is indefinite silence
    underneath a heartbeat still reporting "still watching", which is the worst
    failure this project has: the one that looks like nothing happening.
    """
    return Payload(
        embeds=(
            Embed(
                title=f"⚠️ {labels.name} — an alert could not be delivered",
                description=(
                    f"Discord rejected an alert covering {item_count} "
                    f"{labels.tracked_noun} with HTTP {status_code}, which will not "
                    f"succeed on a retry. Those items are now recorded as seen and "
                    f"will NOT be announced. Polling is unaffected."
                ),
                color=RED,
                footer_text="Check the logs — this usually means the payload was too large.",
            ),
        )
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/lib/test_discord.py -q && uv run mypy --strict lib tests && uv run ruff check .`
Expected: 12 passed, mypy `Success`, ruff clean. Also confirm the library still has no httpx dependency — `grep -r httpx lib/` must return nothing:

```bash
grep -rn httpx lib/ && echo "LEAKED — the transport Protocol is not doing its job" || echo "clean"
```

- [ ] **Step 5: Commit**

```bash
git add lib/monitor/src/monitor/discord.py tests/lib/test_discord.py
git commit -m "Add webhook transport and shared ops messages"
```

---

### Task 7: runner.py part 1 — the Monitor contract and run_tick

This is where divergence #1 lives. Read the spec's "Post-failure semantics" section again before writing the test.

The one design decision to get right: the withholding is **per message**, not per tick. The spec says "withholds the affected message's `covers` keys … banks every other key", and `Message.covers` exists precisely so the runner can tell them apart. Jeffco's TypeScript withholds *every* fresh key on any failure because it has no such handle; the library does better with the one it has.

**Files:**
- Create: `lib/monitor/src/monitor/runner.py`
- Test: `tests/lib/test_runner_tick.py`

**Interfaces:**
- Consumes: `monitor.config.RunnerConfig`, `monitor.discord` (`DiscordPostError`, `Poster`, `StatusPoster`, `format_delivery_failure`), `monitor.types` (`Message`, `Monitor`, `SourceBusy`).
- Produces: `async run_tick[Item](monitor: Monitor[Item], cfg: RunnerConfig, previous_keys: set[str], *, is_first_run: bool, poster: Poster, post_status: StatusPoster, sleep: Callable[[float], Awaitable[None]], log: Callable[[str], None]) -> set[str]` — the new baseline.

Three shape changes from the first draft:

- **It returns `set[str]`, not a `TickResult`.** `run_forever` read only `.keys`; `posted` and `withheld` existed for assertions the poster spy already makes.
- **`Monitor` is imported from `monitor.types`**, not defined here.
- **It takes `post_status`**, because a permanently rejected alert has to tell somebody.

- [ ] **Step 1: Write the failing test**

`tests/lib/test_runner_tick.py`:

```python
from dataclasses import dataclass

import pytest

from monitor.config import RunnerConfig, load_runner_config
from monitor.discord import DiscordPostError
from monitor.runner import run_tick
from monitor.types import Embed, HeartbeatExtras, Message, OpsLabels, Payload

LABELS = OpsLabels(name="X monitor", tracked_noun="thing(s)", death_footer="footer.")


def make_cfg() -> RunnerConfig:
    return load_runner_config(
        {"DISCORD_WEBHOOK_URL": "https://discord.test/webhook"},
        labels=LABELS,
        log_prefix="x-monitor",
        default_poll_interval_sec=10,
    )


@dataclass(frozen=True)
class Thing:
    id: str


def payload_for(*ids: str) -> Payload:
    return Payload(embeds=(Embed(title=" ".join(ids), description="d", color=1),))


class FakeMonitor:
    """One message per item by default, so `covers` isolation is observable.

    `one_message=True` is melanzana's shape: a single message covering every key.
    `cover_only` drops keys from the messages entirely, to exercise the guard
    against a render that silently fails to announce something.
    """

    def __init__(
        self,
        items: list[Thing],
        *,
        one_message: bool = False,
        cover_only: set[str] | None = None,
        render_error: Exception | None = None,
    ) -> None:
        self.items = items
        self.one_message = one_message
        self.cover_only = cover_only
        self.render_error = render_error
        self.rendered: list[list[str]] = []

    async def fetch(self) -> list[Thing]:
        return self.items

    def key(self, item: Thing) -> str:
        return item.id

    async def render(self, new: list[Thing]) -> list[Message]:
        self.rendered.append([t.id for t in new])
        if self.render_error is not None:
            raise self.render_error
        chosen = [t for t in new if self.cover_only is None or t.id in self.cover_only]
        if self.one_message:
            ids = tuple(t.id for t in chosen)
            return [Message(payload=payload_for(*ids), covers=ids)]
        return [Message(payload=payload_for(t.id), covers=(t.id,)) for t in chosen]

    def heartbeat_extras(self) -> HeartbeatExtras:
        return HeartbeatExtras()


class Recorder:
    """A poster that records, and fails on chosen payload titles with a chosen status."""

    def __init__(self, fail_titles: set[str] | None = None, status: int | None = 500) -> None:
        self.posted: list[str] = []
        self.fail_titles = fail_titles or set()
        self.status = status

    async def __call__(self, url: str, payload: Payload) -> None:
        title = payload.embeds[0].title
        if title in self.fail_titles:
            if self.status is None:
                raise RuntimeError(f"connection reset posting {title}")
            raise DiscordPostError(f"Discord webhook failed: HTTP {self.status}", self.status)
        self.posted.append(title)


class StatusRecorder:
    def __init__(self) -> None:
        self.posts: list[Payload] = []

    async def __call__(self, payload: Payload) -> None:
        self.posts.append(payload)


async def noop_sleep(_sec: float) -> None:
    return None


async def drive(
    monitor: FakeMonitor,
    previous: set[str],
    *,
    is_first_run: bool = False,
    poster: Recorder | None = None,
    status: StatusRecorder | None = None,
    sleep: object = noop_sleep,
    logged: list[str] | None = None,
) -> set[str]:
    """Call run_tick with the boilerplate filled in."""
    return await run_tick(
        monitor,
        make_cfg(),
        previous,
        is_first_run=is_first_run,
        poster=poster or Recorder(),
        post_status=status or StatusRecorder(),
        sleep=sleep,  # type: ignore[arg-type]
        log=(logged.append if logged is not None else (lambda _m: None)),
    )


async def test_first_run_records_the_baseline_and_posts_nothing() -> None:
    monitor = FakeMonitor([Thing("a"), Thing("b")])
    poster = Recorder()
    keys = await drive(monitor, set(), is_first_run=True, poster=poster)
    assert poster.posted == []
    assert monitor.rendered == []  # render is never even called
    assert keys == {"a", "b"}


async def test_posts_only_the_items_absent_from_the_previous_key_set() -> None:
    monitor = FakeMonitor([Thing("a"), Thing("b")])
    poster = Recorder()
    keys = await drive(monitor, {"a"}, poster=poster)
    assert monitor.rendered == [["b"]]
    assert poster.posted == ["b"]
    assert keys == {"a", "b"}


async def test_posts_nothing_when_every_item_persists() -> None:
    poster = Recorder()
    keys = await drive(FakeMonitor([Thing("a")]), {"a"}, poster=poster)
    assert poster.posted == []
    assert keys == {"a"}


async def test_one_item_returned_twice_is_announced_once() -> None:
    # Divergence #3. melanzana's padded month queries overlap, so the same slot can
    # arrive twice in one tick; the TypeScript would render "2 open slot(s)" for one
    # slot and print its line twice.
    monitor = FakeMonitor([Thing("a"), Thing("a")], one_message=True)
    poster = Recorder()
    keys = await drive(monitor, set(), poster=poster)
    assert monitor.rendered == [["a"]]
    assert poster.posted == ["a"]
    assert keys == {"a"}


async def test_spaces_the_messages_of_a_batch_but_not_the_first() -> None:
    # The overwhelmingly common tick has exactly one item, and it should reach the
    # phone as fast as it always did.
    slept: list[float] = []

    async def record_sleep(sec: float) -> None:
        slept.append(sec)

    await drive(FakeMonitor([Thing("a"), Thing("b"), Thing("c")]), set(), sleep=record_sleep)
    assert slept == [make_cfg().post_spacing_sec, make_cfg().post_spacing_sec]


async def test_a_failed_post_withholds_only_that_messages_keys() -> None:
    # Divergence #1: the tick does not raise, the unannounced key is withheld so
    # the next tick re-alerts it, and every other key is banked.
    poster = Recorder(fail_titles={"b"}, status=503)
    logged: list[str] = []
    keys = await drive(
        FakeMonitor([Thing("a"), Thing("b"), Thing("c")]), set(), poster=poster, logged=logged
    )
    assert poster.posted == ["a", "c"]
    assert keys == {"a", "c"}
    assert any("will retry 1 item(s)" in line for line in logged)


async def test_a_failed_multi_item_message_withholds_every_key_it_covered() -> None:
    # Melanzana's shape: one message covering every fresh key.
    poster = Recorder(fail_titles={"a b"}, status=503)
    keys = await drive(FakeMonitor([Thing("a"), Thing("b")], one_message=True), set(), poster=poster)
    assert poster.posted == []
    assert keys == set()


async def test_a_transport_error_with_no_status_is_treated_as_retryable() -> None:
    poster = Recorder(fail_titles={"a"}, status=None)
    keys = await drive(FakeMonitor([Thing("a")]), set(), poster=poster)
    assert keys == set()


async def test_a_failed_post_does_not_withhold_a_key_that_still_exists_upstream() -> None:
    # `covers` is subtracted from the *current* key set, so an item that was
    # already in the baseline is unaffected by another message's failure.
    poster = Recorder(fail_titles={"b"}, status=503)
    keys = await drive(FakeMonitor([Thing("a"), Thing("b")]), {"a"}, poster=poster)
    assert poster.posted == []  # only "b" was fresh, and it failed
    assert keys == {"a"}


async def test_a_permanent_rejection_banks_the_keys_and_posts_an_ops_alert() -> None:
    # Retrying a 400 forever is a way of never noticing. Bank the keys so the loop
    # stops, and say out loud that these items will not be announced.
    poster = Recorder(fail_titles={"a"}, status=400)
    status = StatusRecorder()
    logged: list[str] = []
    keys = await drive(FakeMonitor([Thing("a")]), set(), poster=poster, status=status, logged=logged)
    assert keys == {"a"}  # banked, NOT withheld
    assert len(status.posts) == 1
    assert "HTTP 400" in status.posts[0].embeds[0].description
    assert any("permanently rejected" in line for line in logged)


async def test_a_429_abandons_the_rest_of_the_batch() -> None:
    # Continuing at 0.35s spacing would be 2.86 req/s into a webhook that just said
    # stop. The unattempted messages are withheld, so the next tick re-sends them.
    poster = Recorder(fail_titles={"a"}, status=429)
    logged: list[str] = []
    keys = await drive(
        FakeMonitor([Thing("a"), Thing("b"), Thing("c")]), set(), poster=poster, logged=logged
    )
    assert poster.posted == []
    assert keys == set()
    assert any("rate limited" in line for line in logged)


async def test_a_render_failure_withholds_everything_without_failing_the_poll() -> None:
    # The fetch succeeded. Escalating backoff here would eventually post "No
    # successful poll since … may be blocked or down", which is simply untrue.
    monitor = FakeMonitor([Thing("a")], render_error=RuntimeError("detail fetch exploded"))
    logged: list[str] = []
    keys = await drive(monitor, set(), logged=logged)
    assert keys == set()
    assert any("render failed" in line for line in logged)


async def test_a_key_no_message_covers_is_withheld_rather_than_banked() -> None:
    # An app whose render quietly drops an item must not have that item recorded as
    # seen — that is a silent swallow, forever.
    monitor = FakeMonitor([Thing("a"), Thing("b")], cover_only={"a"})
    poster = Recorder()
    keys = await drive(monitor, set(), poster=poster)
    assert poster.posted == ["a"]
    assert keys == {"a"}


async def test_a_fetch_failure_propagates() -> None:
    # No data is not the same as "nothing available"; treating it as an empty list
    # would wipe the baseline and re-alert everything.
    class Broken(FakeMonitor):
        async def fetch(self) -> list[Thing]:
            raise RuntimeError("upstream 503")

    with pytest.raises(RuntimeError, match="503"):
        await drive(Broken([]), set())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/lib/test_runner_tick.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitor.runner'`

- [ ] **Step 3: Write the implementation**

`lib/monitor/src/monitor/runner.py` — this task writes only the contract and `run_tick`; Task 8 appends to the same file:

```python
"""The poll loop. The contract an app implements to be driven by it is in types.py."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from monitor.config import RunnerConfig
from monitor.discord import (
    DiscordPostError,
    Poster,
    StatusPoster,
    format_delivery_failure,
)
from monitor.types import Monitor


async def run_tick[Item](
    monitor: Monitor[Item],
    cfg: RunnerConfig,
    previous_keys: set[str],
    *,
    is_first_run: bool,
    poster: Poster,
    post_status: StatusPoster,
    sleep: Callable[[float], Awaitable[None]],
    log: Callable[[str], None],
) -> set[str]:
    """One poll cycle: fetch, key, diff, render, post. Returns the new baseline.

    On the very first run there is no baseline to diff against, so everything would
    look new. Suppressing here rather than offering a config knob: `echo '[]' >
    state.json` is the supported way to ask for the current backlog, and it makes
    the run not-first by construction.

    A key is left out of the returned baseline — "withheld" — whenever it was not
    successfully announced and a retry might still work. Everything else is banked.
    """
    items = await monitor.fetch()
    current_keys = {monitor.key(item) for item in items}

    if is_first_run:
        return current_keys

    # De-duplicated by key, which the TypeScript does not do. melanzana's month
    # enumeration pads and therefore overlaps, so one slot can arrive twice in a
    # single tick — and a duplicate would inflate the alert's own "N open slot(s)"
    # count and repeat its day-card line. The baseline was always a set and so was
    # never affected; only the rendered message was.
    fresh: list[Item] = []
    fresh_keys: set[str] = set()
    for item in items:
        key = monitor.key(item)
        if key in previous_keys or key in fresh_keys:
            continue
        fresh_keys.add(key)
        fresh.append(item)

    if not fresh:
        return current_keys

    try:
        messages = await monitor.render(fresh)
    except Exception as err:  # noqa: BLE001 — an app-side failure, not a poll failure
        # The fetch succeeded; only the rendering failed. Letting this propagate
        # would escalate backoff and eventually post "No successful poll since …
        # the monitor may be blocked or down" — untrue, and precisely the misleading
        # ops message the post-failure handling exists to avoid. So it is treated
        # exactly like an undelivered post: withhold, keep the cadence, retry.
        current_keys -= fresh_keys
        log(f"render failed ({err}); will retry {len(fresh_keys)} item(s) next tick")
        return current_keys

    settled: set[str] = set()
    posted = 0
    for index, message in enumerate(messages):
        # Not before the first: the overwhelmingly common tick has exactly one item,
        # and it should reach the phone as fast as it always did.
        if index > 0:
            await sleep(cfg.post_spacing_sec)

        try:
            await poster(cfg.alert_webhook_url, message.payload)
        except DiscordPostError as err:
            if not err.retryable:
                # Permanent. Retrying is not caution, it is a way of never noticing:
                # the keys would be withheld every tick forever while the heartbeat
                # kept saying "still watching". Bank them to stop the loop, and say
                # out loud that these items are gone.
                settled.update(message.covers)
                log(
                    f"alert permanently rejected ({err}); {len(message.covers)} "
                    f"item(s) recorded as seen and will NOT be announced"
                )
                await post_status(
                    format_delivery_failure(
                        cfg.labels, err.status_code or 0, len(message.covers)
                    )
                )
                continue

            log(f"alert post failed ({err}); will retry {len(message.covers)} item(s) next tick")
            if err.status_code == 429:
                # Rate-limited. Continuing the batch at post_spacing_sec would be
                # 2.86 req/s into a webhook that just said stop; the rest of the
                # batch is left unsettled and therefore retried next tick.
                log(f"rate limited — abandoning the remaining {len(messages) - index - 1} message(s)")
                break
            continue
        except Exception as err:  # noqa: BLE001 — a transport fault, treated as retryable
            log(f"alert post failed ({err}); will retry {len(message.covers)} item(s) next tick")
            continue

        settled.update(message.covers)
        posted += 1

    # Any fresh key that no message settled was never announced. That includes keys
    # `render` simply omitted from its messages — banking those would swallow the
    # item silently and forever, which is the one outcome this design trades
    # everything else against.
    withheld = fresh_keys - settled
    if withheld:
        current_keys -= withheld
    if posted:
        log(f"posted {posted} message(s) covering {len(fresh) - len(withheld)} new item(s)")

    return current_keys
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/lib/test_runner_tick.py -q && uv run mypy --strict lib tests && uv run ruff check .`
Expected: 14 passed, mypy `Success`, ruff clean. If ruff reports the `noqa: BLE001` comments as *unused*, delete them — `BLE` is not in the selected rule set, and an unused suppression is noise.

- [ ] **Step 5: Commit**

```bash
git add lib/monitor/src/monitor/runner.py tests/lib/test_runner_tick.py
git commit -m "Add the Monitor contract and the poll tick"
```

---

### Task 8: runner.py part 2 — liveness folding and the forever loop

Divergence #2 lives here (the state-write failure). So does the `SourceBusy` cadence hold, which is the third of the three jeffco-driven details in the contract.

**Files:**
- Modify: `lib/monitor/src/monitor/runner.py` (append)
- Test: `tests/lib/test_runner_liveness.py`, `tests/lib/test_runner_forever.py`

**Interfaces:**
- Consumes: everything from Task 7 plus `monitor.health` (`HealthState`, `init_health`, `is_stalled`, `should_heartbeat`), `monitor.discord` (`format_heartbeat`, `format_status_alert`), `monitor.state` (`load_state`, `save_state`), `monitor.timing` (`next_backoff`, `system_now`, `with_jitter`), `monitor.config.make_log`.
- Produces:
  - `async run_liveness(cfg: RunnerConfig, health: HealthState, *, outcome: Literal["success", "failure"], items_tracked: int, now: int, heartbeat_extras: Callable[[], HeartbeatExtras], poster: Poster, log: Callable[[str], None]) -> HealthState`
  - `async run_forever[Item](monitor, cfg, *, poster: Poster, now_unix: Callable[[], int] = system_now, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, rand: Callable[[], float] = random.random, log: Callable[[str], None] | None = None, max_ticks: int | None = None) -> None`
  - `install_shutdown_handlers(log: Callable[[str], None]) -> None`

Four shape changes from the first draft:

- **`heartbeat_extras` is passed as a *callable* and invoked only when a heartbeat is due, inside a guard.** The first draft called `monitor.heartbeat_fields()` eagerly at three sites in `run_forever`, two of them inside `except` branches. An app-side exception there escapes the loop → `main()` exits 1 → `Restart=always` with `RestartSec=10` and `StartLimitBurst=5` → the unit is permanently failed inside a minute, and the only thing that could have raised the alarm is the process that just died.
- **`run_liveness` swallows its own `post_status` failures**, rather than documenting that its caller must. It is exported, and the invariant is load-bearing: an escaping exception leaves `death_alerted` unset, so the death alert re-posts every tick forever. Enforce it where it matters instead of asking.
- **No `state_fs` parameter** — `save_state` is monkeypatched in the one test that needs it to fail.
- **No `install_signal_handlers` flag.** Handler installation moves to `main.py`, which owns the process. The flag was passed `False` by every test and `True` by none of them, which is what a testability workaround looks like.

- [ ] **Step 1: Write the failing liveness test**

`tests/lib/test_runner_liveness.py` — the five `runLiveness` behaviours translated, plus the heartbeat-hour interaction:

```python
from dataclasses import replace

from monitor.config import RunnerConfig, load_runner_config
from monitor.health import HealthState, init_health
from monitor.runner import run_liveness
from monitor.types import Field, HeartbeatExtras, OpsLabels, Payload

LABELS = OpsLabels(name="X monitor", tracked_noun="thing(s)", death_footer="footer.")


def cfg_with(**env: str) -> RunnerConfig:
    return load_runner_config(
        {"DISCORD_WEBHOOK_URL": "https://discord.test/webhook", **env},
        labels=LABELS,
        log_prefix="x-monitor",
        default_poll_interval_sec=10,
    )


def noop_log(_message: str) -> None:
    return None


class Collector:
    """A Poster that records every ops message it is handed."""

    def __init__(self) -> None:
        self.posts: list[Payload] = []

    async def __call__(self, url: str, payload: Payload) -> None:
        self.posts.append(payload)


class ExplodingExtras:
    """An app whose heartbeat_extras() raises — must degrade, never propagate."""

    def __call__(self) -> HeartbeatExtras:
        raise RuntimeError("gap set exploded")


async def test_success_resets_the_failure_count_and_records_the_poll() -> None:
    posts = Collector()
    health = await run_liveness(
        cfg_with(),
        replace(init_health(1000), consecutive_failures=3),
        outcome="success",
        items_tracked=5,
        now=1100,
        heartbeat_extras=HeartbeatExtras,
        poster=posts,
        log=noop_log,
    )
    assert health.consecutive_failures == 0
    assert health.last_success_unix == 1100
    assert health.items_tracked == 5
    assert posts.posts == []


async def test_failure_increments_the_counter_without_alerting_before_the_threshold() -> None:
    posts = Collector()
    health = await run_liveness(
        cfg_with(),
        init_health(1000),
        outcome="failure",
        items_tracked=0,
        now=1100,
        heartbeat_extras=HeartbeatExtras,
        poster=posts,
        log=noop_log,
    )
    assert health.consecutive_failures == 1
    assert health.death_alerted is False
    assert posts.posts == []


async def test_one_death_alert_is_latched_then_one_recovery_is_posted() -> None:
    posts = Collector()
    cfg = cfg_with()
    health: HealthState = init_health(1000)
    for now in (1300, 1700, 2000):  # 300 < 600; 700 >= 600 -> death; then latched
        health = await run_liveness(
            cfg, health, outcome="failure", items_tracked=0, now=now,
            heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
        )
    assert len(posts.posts) == 1
    assert posts.posts[0].content is None  # an ops message never pings
    assert health.death_alerted is True

    health = await run_liveness(
        cfg, health, outcome="success", items_tracked=2, now=2100,
        heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
    )
    assert len(posts.posts) == 2
    assert health.death_alerted is False


async def test_the_heartbeat_waits_for_its_interval() -> None:
    posts = Collector()
    cfg = cfg_with()
    health = await run_liveness(
        cfg, init_health(1000), outcome="success", items_tracked=1, now=1000 + 86399,
        heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
    )
    assert posts.posts == []
    health = await run_liveness(
        cfg, health, outcome="success", items_tracked=1, now=1000 + 86400,
        heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
    )
    assert len(posts.posts) == 1
    assert health.last_heartbeat_unix == 1000 + 86400


async def test_the_heartbeat_is_suppressed_while_latched_dead_and_resumes_after_recovery() -> None:
    # The death alert already signals liveness, and a "still watching" message
    # mid-outage would contradict it. The recovery alert restarts the cadence, so a
    # heartbeat that came due during the outage does not double up right after.
    posts = Collector()
    cfg = cfg_with()
    health = await run_liveness(
        cfg, init_health(1000), outcome="failure", items_tracked=0, now=2000,
        heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
    )
    assert len(posts.posts) == 1  # death only
    health = await run_liveness(
        cfg, health, outcome="failure", items_tracked=0, now=2000 + 86400,
        heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
    )
    assert len(posts.posts) == 1  # still just the death alert
    health = await run_liveness(
        cfg, health, outcome="success", items_tracked=1, now=2000 + 86401,
        heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
    )
    assert len(posts.posts) == 2  # death + recovery, no heartbeat between
    assert health.death_alerted is False
    assert health.last_heartbeat_unix == 2000 + 86401


async def test_a_heartbeat_carries_the_apps_extras_and_honours_the_configured_hour() -> None:
    posts = Collector()
    cfg = cfg_with(HEARTBEAT_AT="07:00")
    dec1_0700, dec1_2300, dec2_0700 = 1796133600, 1796191200, 1796220000
    gap = Field(name="gaps", value="• X", inline=False)
    health = init_health(dec1_0700)

    health = await run_liveness(
        cfg, health, outcome="success", items_tracked=3, now=dec1_2300,
        heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
    )
    assert posts.posts == []  # same local day

    health = await run_liveness(
        cfg, health, outcome="success", items_tracked=3, now=dec2_0700,
        heartbeat_extras=lambda: HeartbeatExtras(fields=(gap,), footer_text="do this"),
        poster=posts, log=noop_log,
    )
    assert len(posts.posts) == 1
    assert posts.posts[0].embeds[0].fields == (gap,)
    assert posts.posts[0].embeds[0].footer_text == "do this"


async def test_extras_that_raise_degrade_to_a_plain_heartbeat() -> None:
    # An app-side error must not take down the loop whose job is to report that the
    # app is alive — that is a permanently failed unit with nothing to raise the alarm.
    posts = Collector()
    logged: list[str] = []
    health = await run_liveness(
        cfg_with(), init_health(1000), outcome="success", items_tracked=1,
        now=1000 + 86400, heartbeat_extras=ExplodingExtras(), poster=posts,
        log=logged.append,
    )
    assert len(posts.posts) == 1  # the heartbeat still went out
    assert posts.posts[0].embeds[0].fields is None
    assert posts.posts[0].embeds[0].footer_text == "Routine heartbeat — no action needed."
    assert health.last_heartbeat_unix == 1000 + 86400
    assert any("heartbeat_extras() raised" in line for line in logged)


async def test_extras_are_not_called_when_no_heartbeat_is_due() -> None:
    # Assembling them can cost real work, so it happens only when one is going out.
    calls = 0

    def counting_extras() -> HeartbeatExtras:
        nonlocal calls
        calls += 1
        return HeartbeatExtras()

    await run_liveness(
        cfg_with(), init_health(1000), outcome="success", items_tracked=1, now=1100,
        heartbeat_extras=counting_extras, poster=Collector(), log=noop_log,
    )
    assert calls == 0


async def test_a_failing_ops_post_cannot_escape_or_strand_the_death_latch() -> None:
    # If this propagated, death_alerted would stay False and the death alert would
    # re-post on every tick forever.
    async def always_fails(url: str, payload: Payload) -> None:
        raise RuntimeError("HTTP 500")

    logged: list[str] = []
    health = await run_liveness(
        cfg_with(STALL_ALERT_SEC="1"), init_health(1000), outcome="failure",
        items_tracked=0, now=2000, heartbeat_extras=HeartbeatExtras,
        poster=always_fails, log=logged.append,
    )
    assert health.death_alerted is True
    assert any("status post failed" in line for line in logged)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/lib/test_runner_liveness.py -q`
Expected: FAIL — `ImportError: cannot import name 'run_liveness' from 'monitor.runner'`

- [ ] **Step 3: Implement run_liveness**

Append to `lib/monitor/src/monitor/runner.py` (and extend the imports at the top of the file to include `asyncio`, `random`, `signal`, `Sequence`, `Literal`, `os`, and the health/discord/state/timing names listed in Interfaces):

```python
async def run_liveness(
    cfg: RunnerConfig,
    health: HealthState,
    *,
    outcome: Literal["success", "failure"],
    items_tracked: int,
    now: int,
    heartbeat_extras: Callable[[], HeartbeatExtras],
    poster: Poster,
    log: Callable[[str], None],
) -> HealthState:
    """Fold a tick outcome into the health state and emit liveness ops messages.

    Returns the next state. Every post here is best-effort and cannot escape: an ops
    message is never worth disturbing the poll loop, and an exception on the way out
    would leave `death_alerted` unset and re-post the death alert every tick forever.
    """

    async def post_status(payload: Payload) -> None:
        try:
            await poster(cfg.status_url, payload)
        except Exception as err:  # noqa: BLE001 — an ops message is never worth a crash
            log(f"status post failed (ignored): {err}")

    def extras() -> HeartbeatExtras:
        # App code, called from inside the loop that reports the app is alive. A
        # raise here must not be the thing that kills the monitor.
        try:
            return heartbeat_extras()
        except Exception as err:  # noqa: BLE001 — degrade the heartbeat, never the loop
            log(f"heartbeat_extras() raised ({err}); sending a plain heartbeat")
            return HeartbeatExtras()

    updated = health

    if outcome == "success":
        updated = replace(
            updated, last_success_unix=now, consecutive_failures=0, items_tracked=items_tracked
        )
        if updated.death_alerted:
            await post_status(format_status_alert("recovery", cfg.labels, updated, now))
            # The recovery message itself signals liveness, so restart the heartbeat
            # cadence from here. Otherwise a heartbeat that came due *during* the
            # outage (last_heartbeat_unix was frozen while latched-dead) would fire
            # immediately after recovery, doubling up on the "I'm alive" signal.
            updated = replace(updated, death_alerted=False, last_heartbeat_unix=now)
    else:
        updated = replace(updated, consecutive_failures=updated.consecutive_failures + 1)
        if is_stalled(updated, now, cfg.stall_alert_sec) and not updated.death_alerted:
            await post_status(format_status_alert("death", cfg.labels, updated, now))
            updated = replace(updated, death_alerted=True)

    # Suppress the heartbeat while latched-dead: the death alert already signals
    # liveness, and a "still watching" message mid-outage would contradict it.
    if not updated.death_alerted and should_heartbeat(
        updated, now, cfg.heartbeat_interval_sec, cfg.heartbeat_at
    ):
        # extras() is called here and nowhere else: only when a heartbeat is actually
        # due, so an app doing real work to assemble it does not do it every tick.
        await post_status(format_heartbeat(cfg.labels, updated, now, extras()))
        updated = replace(updated, last_heartbeat_unix=now)

    return updated
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/lib/test_runner_liveness.py -q`
Expected: 9 passed.

- [ ] **Step 5: Write the failing forever-loop test**

`tests/lib/test_runner_forever.py`:

```python
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from monitor.config import RunnerConfig, load_runner_config
from monitor.runner import run_forever
from monitor.types import Embed, HeartbeatExtras, Message, OpsLabels, Payload, SourceBusy

LABELS = OpsLabels(name="X monitor", tracked_noun="thing(s)", death_footer="footer.")


def cfg_for(state_path: Path, **env: str) -> RunnerConfig:
    return load_runner_config(
        {
            "DISCORD_WEBHOOK_URL": "https://discord.test/webhook",
            "STATE_PATH": str(state_path),
            "POLL_JITTER_PCT": "0",
            **env,
        },
        labels=LABELS,
        log_prefix="x-monitor",
        default_poll_interval_sec=10,
    )


@dataclass(frozen=True)
class Thing:
    id: str


class ScriptedMonitor:
    """Returns a scripted batch per tick; a batch may be an exception to raise."""

    def __init__(self, script: list[list[Thing] | Exception]) -> None:
        self.script = script
        self.tick = 0

    async def fetch(self) -> list[Thing]:
        batch = self.script[min(self.tick, len(self.script) - 1)]
        self.tick += 1
        if isinstance(batch, Exception):
            raise batch
        return batch

    def key(self, item: Thing) -> str:
        return item.id

    async def render(self, new: list[Thing]) -> list[Message]:
        return [
            Message(
                payload=Payload(embeds=(Embed(title=t.id, description="d", color=1),)),
                covers=(t.id,),
            )
            for t in new
        ]

    def heartbeat_extras(self) -> HeartbeatExtras:
        return HeartbeatExtras()


class Harness:
    def __init__(self) -> None:
        self.posted: list[str] = []
        self.slept: list[float] = []
        self.logged: list[str] = []
        self.clock = 1000

    async def poster(self, url: str, payload: Payload) -> None:
        self.posted.append(payload.embeds[0].title)

    async def sleep(self, sec: float) -> None:
        self.slept.append(sec)
        self.clock += int(sec)

    def now(self) -> int:
        return self.clock


async def test_the_loop_baselines_then_alerts_and_persists(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    harness = Harness()
    monitor = ScriptedMonitor([[Thing("a")], [Thing("a"), Thing("b")]])

    await run_forever(
        monitor, cfg_for(state), poster=harness.poster, sleep=harness.sleep,
        now_unix=harness.now, rand=lambda: 0.5, log=harness.logged.append,
        max_ticks=2,
    )

    assert harness.posted == ["b"]  # tick 1 baselines silently, tick 2 alerts
    assert json.loads(state.read_text(encoding="utf-8")) == ["a", "b"]
    assert harness.slept == [10, 10]  # jitter 0 => exactly the interval


async def test_a_fault_escalates_backoff_and_leaves_the_baseline_alone(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text('["a"]', encoding="utf-8")
    harness = Harness()
    monitor = ScriptedMonitor([RuntimeError("upstream 503")])

    await run_forever(
        monitor, cfg_for(state), poster=harness.poster, sleep=harness.sleep,
        now_unix=harness.now, rand=lambda: 0.5, log=harness.logged.append,
        max_ticks=3,
    )

    assert harness.slept == [10, 20, 40]  # doubling from the poll interval
    assert json.loads(state.read_text(encoding="utf-8")) == ["a"]
    assert any("backing off" in line for line in harness.logged)


async def test_source_busy_holds_the_cadence_and_does_not_climb(tmp_path: Path) -> None:
    # SFE answers 400 while the account holder's own session is active. Doubling
    # the gap would blind the monitor for minutes at exactly the moment he is on
    # the site claiming the job the last alert announced.
    harness = Harness()
    monitor = ScriptedMonitor([SourceBusy("HTTP 400 — account busy")])

    await run_forever(
        monitor, cfg_for(tmp_path / "state.json"), poster=harness.poster,
        sleep=harness.sleep, now_unix=harness.now, rand=lambda: 0.5,
        log=harness.logged.append, max_ticks=3,
    )

    assert harness.slept == [10, 10, 10]
    assert any("busy" in line for line in harness.logged)


async def test_source_busy_leaves_an_existing_backoff_ladder_where_it_was(tmp_path: Path) -> None:
    # A real fault arriving later still climbs from where it left off rather than
    # restarting at one interval.
    harness = Harness()
    monitor = ScriptedMonitor(
        [RuntimeError("fault"), SourceBusy("busy"), RuntimeError("fault")]
    )

    await run_forever(
        monitor, cfg_for(tmp_path / "state.json"), poster=harness.poster,
        sleep=harness.sleep, now_unix=harness.now, rand=lambda: 0.5,
        log=harness.logged.append, max_ticks=3,
    )

    assert harness.slept == [10, 10, 20]


async def test_a_failed_post_keeps_the_cadence_and_counts_as_a_healthy_tick(
    tmp_path: Path,
) -> None:
    # Divergence #1, at the loop level: no backoff, no health failure, and the
    # withheld key is re-alerted on the next tick.
    state = tmp_path / "state.json"
    state.write_text("[]", encoding="utf-8")
    harness = Harness()
    monitor = ScriptedMonitor([[Thing("a")]])
    attempts: list[str] = []

    async def flaky_poster(url: str, payload: Payload) -> None:
        title = payload.embeds[0].title
        attempts.append(title)
        if len(attempts) == 1:
            raise RuntimeError("HTTP 500")

    await run_forever(
        monitor, cfg_for(state), poster=flaky_poster, sleep=harness.sleep,
        now_unix=harness.now, rand=lambda: 0.5, log=harness.logged.append,
        max_ticks=2,
    )

    assert attempts == ["a", "a"]  # withheld, then re-alerted
    assert harness.slept == [10, 10]  # normal cadence throughout
    assert json.loads(state.read_text(encoding="utf-8")) == ["a"]


async def test_a_state_write_failure_is_logged_and_the_loop_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Divergence #2. A write failure reported as a *poll* failure would throttle
    # polling 5x, latch a false death alert, and suppress the heartbeat — all while
    # alerts were arriving normally. The in-memory baseline is what the process
    # runs on; durability is best-effort.
    harness = Harness()
    monitor = ScriptedMonitor([[Thing("a")], [Thing("a"), Thing("b")]])

    def unwritable(path: str, keys: set[str]) -> None:
        raise OSError("read-only file system")

    # The string form, not `monkeypatch.setattr(monitor.runner, ...)`: the local
    # variable `monitor` above shadows the module of the same name.
    monkeypatch.setattr("monitor.runner.save_state", unwritable)

    await run_forever(
        monitor, cfg_for(tmp_path / "state.json"), poster=harness.poster,
        sleep=harness.sleep, now_unix=harness.now, rand=lambda: 0.5,
        log=harness.logged.append, max_ticks=2,
    )

    assert harness.posted == ["b"]  # the in-memory baseline advanced, so tick 2 diffed
    assert harness.slept == [10, 10]  # no backoff
    assert any("state write" in line for line in harness.logged)


async def test_a_corrupt_baseline_file_says_so(tmp_path: Path) -> None:
    # Missing and corrupt both read as first run, and they mean opposite things.
    # Corrupt means everything open right now goes unannounced — the failure that
    # looks exactly like everything being fine.
    state = tmp_path / "state.json"
    state.write_text("not json{{{", encoding="utf-8")
    harness = Harness()

    await run_forever(
        ScriptedMonitor([[Thing("a")]]), cfg_for(state), poster=harness.poster,
        sleep=harness.sleep, now_unix=harness.now, rand=lambda: 0.5,
        log=harness.logged.append, max_ticks=1,
    )

    assert any("did not parse" in line for line in harness.logged)


async def test_a_status_post_failure_never_disturbs_polling(tmp_path: Path) -> None:
    # The ops message has to be one that actually fires, which a two-tick run of a
    # healthy monitor never produces: tick 1 is first-run and posts nothing, tick 2
    # diffs empty. SourceBusy plus STALL_ALERT_SEC=1 latches a death alert on tick 2
    # while holding the poll cadence, so the failing post is provably reached and the
    # `[10, 10]` assertion still means something.
    harness = Harness()

    async def always_fails(url: str, payload: Payload) -> None:
        raise RuntimeError("HTTP 500")

    await run_forever(
        ScriptedMonitor([SourceBusy("account busy")]),
        cfg_for(tmp_path / "state.json", STALL_ALERT_SEC="1"),
        poster=always_fails, sleep=harness.sleep, now_unix=harness.now,
        rand=lambda: 0.5, log=harness.logged.append, max_ticks=2,
    )

    assert harness.slept == [10, 10]  # cadence held: no backoff, no crash
    assert any("status post failed" in line for line in harness.logged)
```

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/lib/test_runner_forever.py -q`
Expected: FAIL — `ImportError: cannot import name 'run_forever' from 'monitor.runner'`

- [ ] **Step 7: Implement run_forever**

Append to `lib/monitor/src/monitor/runner.py`:

```python
def _format_at(heartbeat_at: tuple[int, int] | None) -> str:
    return "off" if heartbeat_at is None else f"{heartbeat_at[0]:02d}:{heartbeat_at[1]:02d}"


def install_shutdown_handlers(log: Callable[[str], None]) -> None:
    """Log and exit on SIGTERM/SIGINT. Called by the app's `main`, which owns the
    process — the library is a loop, not a process supervisor.

    Exits immediately rather than draining the current tick. A kill between the post
    and the save re-alerts on restart, which is the trade this whole design makes on
    purpose; waiting for the tick to finish would only delay SIGTERM until Docker's
    kill timeout, and `save_state`'s temp-and-rename means no kill can leave a
    half-written baseline either way.
    """

    def handle(sig: signal.Signals) -> None:
        log(f"{sig.name} received — shutting down")
        raise SystemExit(0)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, functools.partial(handle, sig))


async def run_forever[Item](
    monitor: Monitor[Item],
    cfg: RunnerConfig,
    *,
    poster: Poster,
    now_unix: Callable[[], int] = system_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
    log: Callable[[str], None] | None = None,
    max_ticks: int | None = None,
) -> None:
    """Own the loop: load state, poll, alert, persist, report, sleep.

    `max_ticks` exists so the loop's own wiring — the save, the health folding, the
    choice between jitter and backoff — is testable. Production passes None.
    """
    emit = make_log(cfg.log_prefix) if log is None else log

    loaded = load_state(cfg.state_path)
    first_run = loaded.keys is None
    if loaded.corrupt:
        # Missing and corrupt both re-baseline, and they mean opposite things.
        # Nothing can recover the lost keys, so the only useful response is to say so.
        emit(
            f"{cfg.state_path} exists but did not parse as a key array — "
            f"re-baselining silently; anything open right now will not be announced"
        )

    health = init_health(now_unix())
    emit(
        f"started — interval={cfg.poll_interval_sec}s jitter={cfg.poll_jitter_pct}% "
        f"firstRun={first_run} "
        f"statusChannel={'separate' if cfg.status_webhook_url else 'main'} "
        f"heartbeat={cfg.heartbeat_interval_sec}s "
        f"heartbeatAt={_format_at(cfg.heartbeat_at)} stall={cfg.stall_alert_sec}s"
    )

    async def post_status(payload: Payload) -> None:
        # run_tick's channel for a permanent delivery failure. Swallows its own
        # errors so an ops message can never disturb polling; run_liveness has the
        # same guard for the same reason.
        try:
            await poster(cfg.status_url, payload)
        except Exception as err:  # noqa: BLE001 — an ops message is never worth a crash
            emit(f"status post failed (ignored): {err}")

    previous_keys = loaded.keys or set()
    backoff = 0.0
    ticks = 0

    while max_ticks is None or ticks < max_ticks:
        ticks += 1
        tick_unix = now_unix()
        try:
            current_keys = await run_tick(
                monitor,
                cfg,
                previous_keys,
                is_first_run=first_run,
                poster=poster,
                post_status=post_status,
                sleep=sleep,
                log=emit,
            )
        except SourceBusy as err:
            # A busy signal is not a fault, so it must not compound like one. Hold
            # the normal cadence and leave the escalation ladder where it was, so a
            # real fault arriving later still climbs from where it left off.
            delay = with_jitter(cfg.poll_interval_sec, cfg.poll_jitter_pct, rand)
            emit(f"tick failed (source busy elsewhere), retrying in {round(delay)}s: {err}")
            health = await run_liveness(
                cfg, health, outcome="failure", items_tracked=health.items_tracked,
                now=tick_unix, heartbeat_extras=monitor.heartbeat_extras,
                poster=poster, log=emit,
            )
            await sleep(delay)
            continue
        except Exception as err:  # noqa: BLE001 — one response to every fault
            backoff = next_backoff(backoff, cfg.poll_interval_sec, cfg.max_backoff_sec)
            emit(f"tick failed, backing off {backoff}s: {err}")
            health = await run_liveness(
                cfg, health, outcome="failure", items_tracked=health.items_tracked,
                now=tick_unix, heartbeat_extras=monitor.heartbeat_extras,
                poster=poster, log=emit,
            )
            await sleep(backoff)
            continue

        # The in-memory baseline advances *before* the write is attempted. A
        # persistently unwritable state_path would otherwise re-alert the same items
        # every tick, forever. `first_run` moves for the same reason and matters
        # more: stuck at true, a genuinely new item on a later tick would be
        # silently swallowed.
        previous_keys = current_keys
        first_run = False

        try:
            save_state(cfg.state_path, current_keys)
        except OSError as err:
            # Durability is best-effort. Reporting this as a *poll* failure would
            # throttle polling, latch a false death alert, and suppress the
            # heartbeat, all while alerts were arriving normally.
            emit(
                f"state write to {cfg.state_path} failed ({err}); continuing on the "
                f"in-memory baseline — a restart will re-baseline and silently "
                f"suppress everything currently open, so fix this"
            )

        backoff = 0.0
        health = await run_liveness(
            cfg, health, outcome="success", items_tracked=len(current_keys),
            now=tick_unix, heartbeat_extras=monitor.heartbeat_extras,
            poster=poster, log=emit,
        )
        await sleep(with_jitter(cfg.poll_interval_sec, cfg.poll_jitter_pct, rand))
```

The final import block at the top of `runner.py`:

```python
import asyncio
import functools
import random
import signal
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Literal

from monitor.config import RunnerConfig, make_log
from monitor.discord import (
    DiscordPostError,
    Poster,
    StatusPoster,
    format_delivery_failure,
    format_heartbeat,
    format_status_alert,
)
from monitor.health import HealthState, init_health, is_stalled, should_heartbeat
from monitor.state import load_state, save_state
from monitor.timing import next_backoff, system_now, with_jitter
from monitor.types import HeartbeatExtras, Monitor, Payload, SourceBusy
```

Note what is absent: no `os` (the filesystem lives in `state.py`), no `time` (the clock lives in `timing.py`), and no `httpx` anywhere in the library.

- [ ] **Step 8: Run everything to verify it passes**

Run: `uv run pytest -q && uv run mypy --strict lib tests && uv run ruff check .`
Expected: 8 passed in `test_runner_forever.py`, 97 total across the library, mypy `Success`, ruff clean.

Then confirm the library is a closed set with no stray dependencies:

```bash
grep -rn 'import httpx\|import os\b\|import time\b' lib/ || echo "clean: no transport, no filesystem, no clock outside their own modules"
```

Expected: `state.py` is the only file importing `os`, `timing.py` the only one importing `time`, and nothing imports `httpx`.

- [ ] **Step 9: Commit**

```bash
git add lib/monitor/src/monitor/runner.py tests/lib/test_runner_liveness.py tests/lib/test_runner_forever.py
git commit -m "Add liveness folding and the forever loop"
```

---

### Task 9: melanzana — the fixture and the Cowlendar client

The fixture is copied, never regenerated. It is 663 bytes and it is the only written record of what the API actually returns.

**Files:**
- Create: `tests/melanzana/fixtures/availability-sample.json` (copied)
- Create: `apps/melanzana/src/melanzana/types.py`, `apps/melanzana/src/melanzana/cowlendar.py`
- Test: `tests/melanzana/test_cowlendar.py`

**Interfaces:**
- Consumes: nothing from earlier tasks. Takes a `SourceConfig` protocol so it does not depend on the full config object (which Task 12 builds).
- Produces: `Slot(key: str, start_unix: int, qty_left: int, is_bookable: bool)` frozen dataclass; `CowlendarError(Exception)`; `BASE: str`; `HEADERS: dict[str, str]`; `build_availability_url(calendar_id: str, variant_id: str, timezone: str, year: int, month: int) -> str`; `parse_availability(payload: object) -> list[Slot]`; `async fetch_month(calendar_id: str, variant_id: str, timezone: str, year: int, month: int, client: httpx.AsyncClient) -> list[Slot]`.

Flat string parameters rather than a config object, because that is what makes `build_availability_url` assertable against a pinned expected URL without constructing a whole config.

- [ ] **Step 1: Copy the fixture byte-for-byte and prove it**

```bash
mkdir -p tests/melanzana/fixtures
cp ~/personal/melanzana-monitor/test/fixtures/availability-sample.json \
   tests/melanzana/fixtures/availability-sample.json
cmp ~/personal/melanzana-monitor/test/fixtures/availability-sample.json \
    tests/melanzana/fixtures/availability-sample.json && echo "identical"
wc -c tests/melanzana/fixtures/availability-sample.json
```

Expected: `identical`, and `663` bytes.

- [ ] **Step 2: Write the failing test**

`tests/melanzana/test_cowlendar.py`. The expected URL is not hand-written — it is the exact string Node's `URLSearchParams` produces for the same inputs, verified during planning:

```python
import json
from pathlib import Path

import httpx
import pytest

from melanzana.cowlendar import (
    CowlendarError,
    build_availability_url,
    fetch_month,
    parse_availability,
)
from melanzana.types import Slot

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "availability-sample.json").read_text(encoding="utf-8")
)

# Byte-for-byte what the TypeScript's URLSearchParams emits for these inputs.
EXPECTED_URL = (
    "https://app.cowlendar.com/extapi/calendar/CAL123/availability"
    "?year=2026&month=12&timezone=America%2FDenver"
    "&quantity_details%5B0%5D%5Btype%5D=default"
    "&quantity_details%5B0%5D%5Bquantity%5D=1"
    "&quantity_details%5B0%5D%5Bname%5D=Default"
    "&teammate_id=all&duration=30&is_manual=false&is_pos=false&variant_id=VAR456"
)


def test_the_availability_url_is_byte_identical_to_the_typescript_one() -> None:
    assert build_availability_url("CAL123", "VAR456", "America/Denver", 2026, 12) == EXPECTED_URL


def test_parse_maps_the_long_array_to_slots() -> None:
    slots = parse_availability(FIXTURE)
    assert len(slots) == 3
    assert slots[0] == Slot(
        key="2026-12-01 10:30", start_unix=1796146200, qty_left=4, is_bookable=True
    )
    assert [s.key for s in slots] == [
        "2026-12-01 10:30",
        "2026-12-01 11:00",
        "2026-12-01 11:30",
    ]


def test_parse_returns_nothing_when_long_is_missing_or_not_an_array() -> None:
    assert parse_availability({}) == []
    assert parse_availability({"long": None}) == []
    assert parse_availability({"long": "nope"}) == []
    assert parse_availability("not an object") == []


def test_parse_skips_unusable_rows_rather_than_failing_the_poll() -> None:
    # A garbage row is a row, not an outage. Dropping it keeps the other slots
    # alertable; raising would turn one malformed entry into a stalled monitor.
    payload = {
        "long": [
            "not an object",
            {"slot": "", "slot_start_unix": 1796146200},
            {"slot": "2026-12-01 10:30", "slot_start_unix": 0},
            {"slot": "2026-12-01 11:00", "slot_start_unix": "garbage"},
            {"slot": "2026-12-01 11:30", "slot_start_unix": 1796149800},
        ]
    }
    assert [s.key for s in parse_availability(payload)] == ["2026-12-01 11:30"]


def test_parse_defaults_a_missing_quantity_and_flag() -> None:
    payload = {"long": [{"slot": "2026-12-01 10:30", "slot_start_unix": 1796146200}]}
    slot = parse_availability(payload)[0]
    assert slot.qty_left == 0
    assert slot.is_bookable is False


async def test_fetch_month_returns_parsed_slots() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, json=FIXTURE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        slots = await fetch_month("CAL123", "VAR456", "America/Denver", 2026, 12, client)

    assert [s.key for s in slots] == [
        "2026-12-01 10:30",
        "2026-12-01 11:00",
        "2026-12-01 11:30",
    ]
    assert seen["url"] == EXPECTED_URL
    assert "Chrome" in seen["ua"]  # the widget's own headers, to reduce block risk


async def test_fetch_month_raises_on_a_non_ok_response() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(429))
    ) as client:
        with pytest.raises(CowlendarError, match="429"):
            await fetch_month("CAL123", "VAR456", "America/Denver", 2026, 12, client)
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/melanzana/test_cowlendar.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'melanzana.cowlendar'`

- [ ] **Step 4: Write the implementation**

`apps/melanzana/src/melanzana/types.py`:

```python
"""Melanzana's item type. The library never inspects it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Slot:
    """A single appointment slot, normalized from the Cowlendar API."""

    #: The API `slot` string, e.g. "2026-12-01 10:30". Identity key AND display.
    key: str
    #: The API `slot_start_unix`, epoch seconds. Used for all window/time math.
    start_unix: int
    #: The API `qty_left` — spots remaining.
    qty_left: int
    #: The API `is_bookable`.
    is_bookable: bool
```

`apps/melanzana/src/melanzana/cowlendar.py`:

```python
"""The Cowlendar availability endpoint. No auth of any kind: no token, no cookie,
no session — which is why melanzana never needed the abstraction jeffco does."""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlencode

import httpx

from melanzana.types import Slot

BASE = "https://app.cowlendar.com/extapi/calendar"

#: Browser-mimicking headers copied from the real widget request, which reduces
#: bot/block risk. Kept verbatim from the TypeScript client.
HEADERS: dict[str, str] = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://widget.cowlendar.com",
    "referer": "https://widget.cowlendar.com/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
}


class CowlendarError(Exception):
    """The availability request was rejected. A fault, not a busy signal — the
    runner backs off. Cowlendar has no equivalent of SmartFindExpress's 400."""


def build_availability_url(
    calendar_id: str, variant_id: str, timezone: str, year: int, month: int
) -> str:
    """One month's availability URL.

    `urlencode` matches `URLSearchParams` byte for byte on these inputs — both
    percent-encode the brackets and the slash in the zone identifier, both preserve
    insertion order. Verified against node during planning, and pinned in the test.
    """
    params = {
        "year": str(year),
        "month": str(month),
        "timezone": timezone,
        "quantity_details[0][type]": "default",
        "quantity_details[0][quantity]": "1",
        "quantity_details[0][name]": "Default",
        "teammate_id": "all",
        "duration": "30",
        "is_manual": "false",
        "is_pos": "false",
        "variant_id": variant_id,
    }
    return f"{BASE}/{calendar_id}/availability?{urlencode(params)}"


def _to_int(value: object) -> int | None:
    """A tolerant integer read. None when the value cannot be one.

    The TypeScript relies on `Number(x)` producing NaN and a later `> 0` filter
    dropping it. Python raises instead, so the coercion is explicit here and the
    caller skips the row — same outcome, visible reason.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def parse_availability(payload: object) -> list[Slot]:
    """Normalize the API payload into Slots. Tolerant of a missing or garbage `long`.

    A row without a usable key or start time is skipped rather than raised on: a
    malformed entry is one lost slot, whereas raising would stall the monitor and
    lose all of them.
    """
    if not isinstance(payload, dict):
        return []
    long: Any = payload.get("long")
    if not isinstance(long, list):
        return []

    slots: list[Slot] = []
    for row in long:
        if not isinstance(row, dict):
            continue
        raw_key = row.get("slot")
        key = "" if raw_key is None else str(raw_key)
        start_unix = _to_int(row.get("slot_start_unix"))
        if key == "" or start_unix is None or start_unix <= 0:
            continue
        qty_left = _to_int(row.get("qty_left"))
        slots.append(
            Slot(
                key=key,
                start_unix=start_unix,
                qty_left=0 if qty_left is None else qty_left,
                is_bookable=bool(row.get("is_bookable")),
            )
        )
    return slots


async def fetch_month(
    calendar_id: str,
    variant_id: str,
    timezone: str,
    year: int,
    month: int,
    client: httpx.AsyncClient,
) -> list[Slot]:
    """Fetch one month of availability. Raises `CowlendarError` on a non-2xx."""
    url = build_availability_url(calendar_id, variant_id, timezone, year, month)
    response = await client.get(url, headers=HEADERS)
    if not 200 <= response.status_code < 300:
        raise CowlendarError(f"Cowlendar request failed: HTTP {response.status_code}")
    return parse_availability(response.json())
```

- [ ] **Step 5: Run it to verify it passes**

Run: `uv run pytest tests/melanzana/test_cowlendar.py -q && uv run mypy --strict apps tests && uv run ruff check .`
Expected: 7 passed, mypy `Success`, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add tests/melanzana apps/melanzana/src/melanzana/types.py apps/melanzana/src/melanzana/cowlendar.py
git commit -m "Port the Cowlendar client and reuse its fixture verbatim"
```

---

### Task 10: melanzana — month enumeration and the window cut

All UTC, exactly as the TypeScript. This is the module the spec singles out as *why* melanzana is the safe first port: no `Intl`, no local time, no DST.

**Files:**
- Create: `apps/melanzana/src/melanzana/detector.py`
- Test: `tests/melanzana/test_detector.py`

**Interfaces:**
- Consumes: `melanzana.types.Slot`.
- Produces: `MonthKey(year: int, month: int)` frozen dataclass; `months_to_fetch(now_unix: int, window_days: int) -> list[MonthKey]`; `filter_bookable(slots: Sequence[Slot], now_unix: int, window_days: int) -> list[Slot]`.

`detect_new` is deliberately *not* here — the key-set diff moved into the library's `run_tick`, which is where it is shared with jeffco.

- [ ] **Step 1: Write the failing test**

`tests/melanzana/test_detector.py`:

```python
from melanzana.detector import MonthKey, filter_bookable, months_to_fetch
from melanzana.types import Slot

NOW = 1796000000  # 2026-11-30 00:53 UTC
DAY = 86400


def slot(key: str, start_unix: int, qty_left: int = 4, is_bookable: bool = True) -> Slot:
    return Slot(key=key, start_unix=start_unix, qty_left=qty_left, is_bookable=is_bookable)


def test_enumerates_every_month_the_window_spans_with_pads_on_both_ends() -> None:
    assert months_to_fetch(NOW, 60) == [
        MonthKey(2026, 11),
        MonthKey(2026, 12),
        MonthKey(2027, 1),
    ]


def test_includes_the_prior_denver_month_at_a_utc_month_boundary() -> None:
    # 2026-12-01 03:00 UTC is still 2026-11-30 in Denver (UTC-7); a near-term slot
    # would be in the NOVEMBER api query. The start pad must cover it.
    boundary = 1796094000
    assert months_to_fetch(boundary, 60) == [
        MonthKey(2026, 11),
        MonthKey(2026, 12),
        MonthKey(2027, 1),
        MonthKey(2027, 2),
    ]


def test_rolls_the_year_over_at_december() -> None:
    assert months_to_fetch(NOW, 400)[-1] == MonthKey(2028, 1)


def test_keeps_only_in_window_bookable_slots_with_spots_left() -> None:
    slots = [
        slot("2026-12-01 10:30", 1796146200, 4, True),  # in window, bookable -> KEEP
        slot("2026-12-01 11:00", 1796148000, 0, True),  # qty 0 -> DROP
        slot("2026-12-01 11:30", 1796149800, 4, False),  # not bookable -> DROP
        slot("past", NOW - DAY, 4, True),  # before now -> DROP
        slot("far", NOW + 61 * DAY, 4, True),  # beyond 60d -> DROP
    ]
    assert [s.key for s in filter_bookable(slots, NOW, 60)] == ["2026-12-01 10:30"]


def test_the_window_boundaries_are_inclusive() -> None:
    slots = [slot("now", NOW), slot("edge", NOW + 60 * DAY)]
    assert [s.key for s in filter_bookable(slots, NOW, 60)] == ["now", "edge"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/melanzana/test_detector.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'melanzana.detector'`

- [ ] **Step 3: Write the implementation**

`apps/melanzana/src/melanzana/detector.py`:

```python
"""Which months to ask for, and which of the returned slots count.

Every arithmetic operation here is UTC, matching the TypeScript's `getUTCMonth`
and `getUTCFullYear`. Melanzana has no local-time or DST logic anywhere, which is
what makes it the low-risk first port.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from melanzana.types import Slot

DAY_SEC = 86400
#: Two days on both ends. The pads absorb the gap between UTC month enumeration and
#: the API's local (Denver) date grouping: e.g. early on the 1st UTC it is still the
#: prior day locally, so a near-term slot can sit in the previous month's query.
#: Over-fetching a boundary month is harmless — filter_bookable does the precise cut.
PAD_DAYS = 2


@dataclass(frozen=True)
class MonthKey:
    year: int
    month: int  # 1-12


def months_to_fetch(now_unix: int, window_days: int) -> list[MonthKey]:
    """Every calendar month (UTC) the window spans, padded on both ends."""
    start = datetime.fromtimestamp(now_unix - PAD_DAYS * DAY_SEC, UTC)
    end = datetime.fromtimestamp(now_unix + (window_days + PAD_DAYS) * DAY_SEC, UTC)

    out: list[MonthKey] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append(MonthKey(year, month))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return out


def filter_bookable(slots: Sequence[Slot], now_unix: int, window_days: int) -> list[Slot]:
    """Slots that are bookable, have spots left, and start within [now, now + window]."""
    window_end = now_unix + window_days * DAY_SEC
    return [
        s
        for s in slots
        if s.is_bookable and s.qty_left > 0 and now_unix <= s.start_unix <= window_end
    ]
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/melanzana/test_detector.py -q && uv run mypy --strict apps tests && uv run ruff check .`
Expected: 5 passed, mypy `Success`, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add apps/melanzana/src/melanzana/detector.py tests/melanzana/test_detector.py
git commit -m "Port month enumeration and the bookable-window cut"
```

---

### Task 11: melanzana — the day-card alert

The only output a user ever sees. Field ordering, number formatting, the em-dash in `"14:00 — 2 left"`, the `@everyone`/`allowed_mentions` pair — all of it is asserted here and then re-checked against the TypeScript byte-for-byte in Task 13.

**Files:**
- Create: `apps/melanzana/src/melanzana/alert.py`
- Test: `tests/melanzana/test_alert.py`

**Interfaces:**
- Consumes: `melanzana.types.Slot`, `monitor.types` (`GREEN`, `Embed`, `Field`, `Payload`). **Nothing from `monitor.discord`** — a pure renderer should not pull in the transport.
- Produces: `MAX_DAY_CARDS: int = 24`; `format_alert(slots: Sequence[Slot], *, booking_url: str, mention_everyone: bool) -> Payload`.

Keyword arguments rather than the config object so the parity dump script can call it without building one.

- [ ] **Step 1: Write the failing test**

`tests/melanzana/test_alert.py`:

```python
from melanzana.alert import format_alert
from melanzana.types import Slot
from monitor.types import GREEN

BOOKING_URL = "https://melanzana.com/pages/how-to-shop"


def slot(key: str, qty_left: int = 4) -> Slot:
    return Slot(key=key, start_unix=1796146200, qty_left=qty_left, is_bookable=True)


def test_groups_slots_into_one_inline_day_card_per_day_with_a_summary() -> None:
    payload = format_alert(
        [slot("2026-12-01 10:30", 4), slot("2026-12-01 11:00", 2), slot("2026-12-02 09:00", 5)],
        booking_url=BOOKING_URL,
        mention_everyone=True,
    )
    embed = payload.embeds[0]
    assert embed.title == "🟢 New Melanzana appointment(s) available!"
    assert embed.url == BOOKING_URL
    assert embed.description == "3 open slot(s) across 2 day(s):"
    assert embed.color == GREEN
    assert embed.footer_text == "Tap the title to book — slots go fast."
    assert embed.fields is not None
    assert len(embed.fields) == 2
    # 2026-12-01 is a Tuesday, 2026-12-02 a Wednesday (UTC).
    assert embed.fields[0].name == "📅 Tue, Dec 1"
    assert embed.fields[0].value == "10:30 — 4 left\n11:00 — 2 left"
    assert embed.fields[0].inline is True
    assert embed.fields[1].name == "📅 Wed, Dec 2"
    assert embed.fields[1].value == "09:00 — 5 left"


def test_orders_days_and_times_chronologically_regardless_of_input_order() -> None:
    payload = format_alert(
        [slot("2026-12-02 09:00"), slot("2026-12-01 11:00"), slot("2026-12-01 10:30")],
        booking_url=BOOKING_URL,
        mention_everyone=True,
    )
    fields = payload.embeds[0].fields
    assert fields is not None
    assert [f.name for f in fields] == ["📅 Tue, Dec 1", "📅 Wed, Dec 2"]
    assert fields[0].value == "10:30 — 4 left\n11:00 — 4 left"


def test_adds_the_everyone_pair_when_mentioning() -> None:
    payload = format_alert(
        [slot("2026-12-01 10:30")], booking_url=BOOKING_URL, mention_everyone=True
    )
    assert payload.content == "@everyone"
    assert payload.allowed_mentions_parse == ("everyone",)


def test_omits_the_mention_when_not_mentioning() -> None:
    payload = format_alert(
        [slot("2026-12-01 10:30")], booking_url=BOOKING_URL, mention_everyone=False
    )
    assert payload.content is None
    assert payload.allowed_mentions_parse is None
    assert "content" not in payload.to_dict()
    assert "allowed_mentions" not in payload.to_dict()


def test_caps_the_day_cards_and_notes_the_remaining_days() -> None:
    # 30 distinct days (Sep 1-30) -> 24 day-cards + 1 overflow note field.
    # Discord allows 25 embed fields; one is reserved for the note.
    many = [slot(f"2026-09-{day:02d} 10:00") for day in range(1, 31)]
    fields = format_alert(many, booking_url=BOOKING_URL, mention_everyone=True).embeds[0].fields
    assert fields is not None
    assert len(fields) == 25
    assert fields[24].name == "…"
    assert fields[24].value == "and 6 more day(s) — tap the title to see all."
    assert fields[24].inline is False


def test_falls_back_gracefully_for_a_key_without_a_parseable_date() -> None:
    fields = format_alert(
        [slot("weird-key")], booking_url=BOOKING_URL, mention_everyone=True
    ).embeds[0].fields
    assert fields is not None
    assert fields[0].name == "📅 weird-key"
    assert fields[0].value == "weird-key — 4 left"


def test_accepts_the_iso_t_separator_in_a_key() -> None:
    fields = format_alert(
        [slot("2026-12-01T10:30")], booking_url=BOOKING_URL, mention_everyone=False
    ).embeds[0].fields
    assert fields is not None
    assert fields[0].name == "📅 Tue, Dec 1"
    assert fields[0].value == "10:30 — 4 left"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/melanzana/test_alert.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'melanzana.alert'`

- [ ] **Step 3: Write the implementation**

`apps/melanzana/src/melanzana/alert.py`:

```python
"""The day-card embed. Alert formatting is per-app by design: melanzana renders one
embed with a field per day, jeffco renders one message per job."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from melanzana.types import Slot
from monitor.types import GREEN, Embed, Field, Payload

#: Discord allows 25 embed fields; one is reserved for the overflow note.
MAX_DAY_CARDS = 24

WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

_KEY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2})")


def _parse_key(key: str) -> tuple[str, str, str]:
    """Split a slot key ("YYYY-MM-DD HH:MM") into (day bucket, day header, time).

    Display only — the original slot key stays the diff identity. A key that does
    not start with a date falls back to the raw key, with no grouping magic.

    The TypeScript anchors its `Date` at noon UTC so the weekday cannot cross a day
    boundary; a Python `date` has no time component, so no anchor is needed and the
    weekday is the same. `isoweekday() % 7` converts Python's Monday=1 to
    JavaScript's `getUTCDay()` Sunday=0.
    """
    match = _KEY.match(key)
    if match is None:
        return key, f"📅 {key}", key
    year, month, day, time = match.groups()
    weekday = WEEKDAYS[date(int(year), int(month), int(day)).isoweekday() % 7]
    return (
        f"{year}-{month}-{day}",
        f"📅 {weekday}, {MONTHS[int(month) - 1]} {int(day)}",
        time,
    )


def format_alert(
    slots: Sequence[Slot], *, booking_url: str, mention_everyone: bool
) -> Payload:
    """One embed for a batch of newly-available slots, rendered as an inline day
    card per day (time — spots left) in chronological order."""
    by_day: dict[str, tuple[str, list[str]]] = {}
    for s in slots:
        day_key, header, time = _parse_key(s.key)
        _, lines = by_day.setdefault(day_key, (header, []))
        lines.append(f"{time} — {s.qty_left} left")

    # Sorting the day keys: every key is either a same-shape ISO date or a raw
    # fallback, and for those `localeCompare` and code-point order agree. Sorting
    # the lines works for the same reason — zero-padded HH:MM sorts chronologically.
    days = sorted(by_day.items(), key=lambda item: item[0])

    fields = [
        Field(name=header, value="\n".join(sorted(lines)), inline=True)
        for _, (header, lines) in days[:MAX_DAY_CARDS]
    ]
    if len(days) > MAX_DAY_CARDS:
        fields.append(
            Field(
                name="…",
                value=(
                    f"and {len(days) - MAX_DAY_CARDS} more day(s) — "
                    f"tap the title to see all."
                ),
                inline=False,
            )
        )

    payload = Payload(
        embeds=(
            Embed(
                title="🟢 New Melanzana appointment(s) available!",
                url=booking_url,
                description=f"{len(slots)} open slot(s) across {len(days)} day(s):",
                color=GREEN,
                fields=tuple(fields),
                footer_text="Tap the title to book — slots go fast.",
            ),
        )
    )
    if mention_everyone:
        payload = replace(payload, content="@everyone", allowed_mentions_parse=("everyone",))
    return payload
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/melanzana/test_alert.py -q && uv run mypy --strict apps tests && uv run ruff check .`
Expected: 7 passed, mypy `Success`, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add apps/melanzana/src/melanzana/alert.py tests/melanzana/test_alert.py
git commit -m "Port the day-card alert embed"
```

---

### Task 12: melanzana — config, the Monitor implementation, and main

The four contract methods, and the wiring. After this task the app runs.

**Files:**
- Create: `apps/melanzana/src/melanzana/config.py`, `apps/melanzana/src/melanzana/monitor.py`, `apps/melanzana/src/melanzana/main.py`
- Create: `apps/melanzana/.env.example`
- Test: `tests/melanzana/test_config.py`, `tests/melanzana/test_monitor.py`

**Interfaces:**
- Consumes: `monitor.config` (`Env`, `RunnerConfig`, `env_bool`, `env_num`, `env_str`, `load_runner_config`, `make_log`), `monitor.types` (`HeartbeatExtras`, `Message`, `OpsLabels`), `monitor.timing.system_now`, `monitor.runner` (`install_shutdown_handlers`, `run_forever`), `monitor.discord.post`, `melanzana.cowlendar.fetch_month`, `melanzana.detector` (`filter_bookable`, `months_to_fetch`), `melanzana.alert.format_alert`.
- Produces: `LOG_PREFIX: str`; `LABELS: OpsLabels`; `MelanzanaConfig` frozen dataclass with `runner: RunnerConfig`, `calendar_id: str`, `variant_id: str`, `window_days: int`, `timezone: str`, `booking_url: str`, `mention_everyone: bool`; `load_config(env: Env) -> MelanzanaConfig`; `MelanzanaMonitor(cfg: MelanzanaConfig, client: httpx.AsyncClient, now_unix: Callable[[], int] = system_now)` implementing `Monitor[Slot]`; `main() -> None`.

`main.py` is the one file that is genuinely per-app boilerplate, and it stays that way on purpose: it is where an app constructs its HTTP client, and fashionjobs must construct a `curl_cffi` session instead of an `httpx.AsyncClient`. Hoisting it into a library `run_app()` would put the one thing app #3 must change behind a shared abstraction.

- [ ] **Step 1: Write the failing config test**

`tests/melanzana/test_config.py`:

```python
import pytest

from melanzana.config import LABELS, load_config
from monitor.config import ConfigError

WEBHOOK = "https://discord.test/webhook"


def test_applies_every_default_when_only_the_webhook_is_provided() -> None:
    cfg = load_config({"DISCORD_WEBHOOK_URL": WEBHOOK})
    assert cfg.calendar_id == "685b42f202405a8372cd6b78"
    assert cfg.variant_id == "41855678382123"
    assert cfg.window_days == 60
    assert cfg.timezone == "America/Denver"
    assert cfg.booking_url == "https://melanzana.com/pages/how-to-shop"
    assert cfg.mention_everyone is False
    assert cfg.runner.poll_interval_sec == 10
    assert cfg.runner.poll_jitter_pct == 20
    assert cfg.runner.state_path == "/data/state.json"
    assert cfg.runner.heartbeat_interval_sec == 86400
    assert cfg.runner.stall_alert_sec == 600
    # Unset by default, which is what keeps the port at parity: the heartbeat
    # behaves exactly as it does today until Terraform sets the hour.
    assert cfg.runner.heartbeat_at is None
    assert cfg.runner.status_webhook_url is None


def test_parses_overrides_from_the_environment() -> None:
    cfg = load_config(
        {
            "DISCORD_WEBHOOK_URL": WEBHOOK,
            "POLL_INTERVAL_SEC": "30",
            "WINDOW_DAYS": "90",
            "MENTION_EVERYONE": "true",
            "HEARTBEAT_AT": "07:00",
        }
    )
    assert cfg.runner.poll_interval_sec == 30
    assert cfg.window_days == 90
    assert cfg.mention_everyone is True
    assert cfg.runner.heartbeat_at == (7, 0)


def test_the_library_validation_is_actually_wired_in() -> None:
    # One test that the primitives are reached, not a second copy of their own
    # suite — env_num and env_https_url are covered in tests/lib/test_config.py.
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL is required"):
        load_config({})
    with pytest.raises(ConfigError, match="WINDOW_DAYS"):
        load_config({"DISCORD_WEBHOOK_URL": WEBHOOK, "WINDOW_DAYS": "0"})


def test_the_ops_labels_are_melanzanas() -> None:
    # These exact strings are parity-critical: the differential harness renders the
    # heartbeat and both status alerts from them, so a typo here surfaces as a diff
    # pointing at the library rather than at this line.
    assert LABELS.name == "Melanzana monitor"
    assert LABELS.tracked_noun == "slot(s)"
    assert LABELS.death_footer == "Liveness alert — the monitor may be blocked or down."
    assert load_config({"DISCORD_WEBHOOK_URL": WEBHOOK}).runner.labels is LABELS
```

- [ ] **Step 2: Write the failing monitor test**

`tests/melanzana/test_monitor.py`:

```python
import httpx

from melanzana.config import load_config
from melanzana.monitor import MelanzanaMonitor
from melanzana.types import Slot

WEBHOOK = "https://discord.test/webhook"
NOW = 1796000000
FIXTURE_SLOT = {
    "slot": "2026-12-01 10:30",
    "slot_start_unix": 1796146200,
    "is_bookable": True,
    "qty_left": 4,
}


def make_monitor(
    handler: httpx.MockTransport, **env: str
) -> tuple[MelanzanaMonitor, httpx.AsyncClient]:
    cfg = load_config({"DISCORD_WEBHOOK_URL": WEBHOOK, **env})
    client = httpx.AsyncClient(transport=handler)
    return MelanzanaMonitor(cfg, client=client, now_unix=lambda: NOW), client


def test_the_key_is_the_slot_string() -> None:
    monitor, _ = make_monitor(httpx.MockTransport(lambda _r: httpx.Response(200, json={})))
    slot = Slot(key="2026-12-01 10:30", start_unix=1796146200, qty_left=4, is_bookable=True)
    assert monitor.key(slot) == "2026-12-01 10:30"


async def test_fetch_queries_every_month_in_the_window_and_returns_only_bookables() -> None:
    queried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queried.append(f"{request.url.params['year']}-{request.url.params['month']}")
        return httpx.Response(
            200,
            json={
                "long": [
                    FIXTURE_SLOT,
                    # Dropped by the window cut, so it must not reach the runner.
                    {
                        "slot": "2026-12-01 12:00",
                        "slot_start_unix": 1796146200,
                        "is_bookable": True,
                        "qty_left": 0,
                    },
                ]
            },
        )

    monitor, client = make_monitor(httpx.MockTransport(handler))
    async with client:
        items = await monitor.fetch()

    assert sorted(queried) == ["2026-11", "2026-12", "2027-1"]
    # The mock answers every month query with the same slot, so fetch() legitimately
    # returns it three times — the padded months overlap and nothing here de-dupes.
    # That is exactly the case run_tick now collapses (divergence #3); asserting the
    # list rather than a set keeps the boundary honest about who owns it.
    assert [s.key for s in items] == ["2026-12-01 10:30"] * 3


async def test_render_returns_one_message_covering_every_fresh_key() -> None:
    monitor, client = make_monitor(
        httpx.MockTransport(lambda _r: httpx.Response(200, json={})), MENTION_EVERYONE="true"
    )
    fresh = [
        Slot(key="2026-12-01 10:30", start_unix=1796146200, qty_left=4, is_bookable=True),
        Slot(key="2026-12-02 09:00", start_unix=1796232600, qty_left=5, is_bookable=True),
    ]
    async with client:
        messages = await monitor.render(fresh)

    assert len(messages) == 1
    assert messages[0].covers == ("2026-12-01 10:30", "2026-12-02 09:00")
    assert messages[0].payload.content == "@everyone"
    assert messages[0].payload.embeds[0].description == "2 open slot(s) across 2 day(s):"


def test_heartbeat_extras_are_empty_because_melanzana_has_nothing_to_add() -> None:
    monitor, _ = make_monitor(httpx.MockTransport(lambda _r: httpx.Response(200, json={})))
    extras = monitor.heartbeat_extras()
    assert extras.fields == ()
    assert extras.footer_text is None  # keeps the library's default footer


async def test_a_month_query_failure_propagates_so_the_runner_backs_off() -> None:
    import pytest

    from melanzana.cowlendar import CowlendarError

    monitor, client = make_monitor(httpx.MockTransport(lambda _r: httpx.Response(503)))
    async with client, pytest.raises(CowlendarError, match="503"):
        await monitor.fetch()
```

- [ ] **Step 3: Run both to verify they fail**

Run: `uv run pytest tests/melanzana/test_config.py tests/melanzana/test_monitor.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'melanzana.config'`

- [ ] **Step 4: Write the implementation**

`apps/melanzana/src/melanzana/config.py`:

```python
"""Melanzana's schema. The library owns the shared names; these are the ones only
this app reads."""

from __future__ import annotations

from dataclasses import dataclass

from monitor.config import Env, RunnerConfig, env_bool, env_num, env_str, load_runner_config
from monitor.types import OpsLabels

#: Written once. It is the log prefix, and `main.py` reads it from here rather than
#: repeating the string in a print().
LOG_PREFIX = "melanzana-monitor"

LABELS = OpsLabels(
    name="Melanzana monitor",
    tracked_noun="slot(s)",
    death_footer="Liveness alert — the monitor may be blocked or down.",
)


@dataclass(frozen=True)
class MelanzanaConfig:
    runner: RunnerConfig
    calendar_id: str
    variant_id: str
    window_days: int
    #: The zone Cowlendar groups its slots by — a *request parameter*, not the
    #: operator's clock. HEARTBEAT_AT is interpreted in monitor.health.OPERATOR_TZ
    #: and is deliberately unrelated to this.
    timezone: str
    booking_url: str
    mention_everyone: bool


def load_config(env: Env) -> MelanzanaConfig:
    """Build a validated config from the environment. Raises on anything unusable."""
    return MelanzanaConfig(
        runner=load_runner_config(
            env,
            labels=LABELS,
            log_prefix=LOG_PREFIX,
            # 10s. Cowlendar needs no auth and has no session to collide with, so
            # unlike jeffco there is no reason to poll slower.
            default_poll_interval_sec=10,
        ),
        calendar_id=env_str(env, "CALENDAR_ID", "685b42f202405a8372cd6b78"),
        variant_id=env_str(env, "VARIANT_ID", "41855678382123"),
        window_days=int(env_num(env, "WINDOW_DAYS", 60)),
        timezone=env_str(env, "TIMEZONE", "America/Denver"),
        booking_url=env_str(env, "BOOKING_URL", "https://melanzana.com/pages/how-to-shop"),
        mention_everyone=env_bool(env, "MENTION_EVERYONE", False),
    )
```

`apps/melanzana/src/melanzana/monitor.py`:

```python
"""The four contract methods. Everything the library does not own lives here."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx

from melanzana.alert import format_alert
from melanzana.config import MelanzanaConfig
from melanzana.cowlendar import fetch_month
from melanzana.detector import filter_bookable, months_to_fetch
from melanzana.types import Slot
from monitor.timing import system_now
from monitor.types import HeartbeatExtras, Message


class MelanzanaMonitor:
    """Implements `monitor.runner.Monitor[Slot]`.

    Holds no state between ticks — melanzana has none to hold. The class shape comes
    from jeffco, which caches a token and accumulates filter gaps.
    """

    def __init__(
        self,
        cfg: MelanzanaConfig,
        client: httpx.AsyncClient,
        now_unix: Callable[[], int] = system_now,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._now_unix = now_unix

    async def fetch(self) -> list[Slot]:
        """Every bookable slot inside the window, right now.

        One `now` for the whole tick: month selection and the window cut must agree,
        and a clock read between them could disagree at a boundary.

        Months are fetched concurrently. A failure in any one of them propagates —
        no data is not the same as "nothing available", and treating a partial
        result as complete would bank the missing slots as gone and re-alert them
        all on the next success.
        """
        now = self._now_unix()
        months = months_to_fetch(now, self._cfg.window_days)
        fetched = await asyncio.gather(
            *(
                fetch_month(
                    self._cfg.calendar_id,
                    self._cfg.variant_id,
                    self._cfg.timezone,
                    month.year,
                    month.month,
                    self._client,
                )
                for month in months
            )
        )
        slots = [slot for month_slots in fetched for slot in month_slots]
        return filter_bookable(slots, now, self._cfg.window_days)

    def key(self, item: Slot) -> str:
        """The API's own slot string. Identity and display are the same value."""
        return item.key

    async def render(self, new: list[Slot]) -> list[Message]:
        """One message covering every fresh key.

        Async because the contract is — jeffco enriches per item here. Melanzana has
        nothing to fetch, so this never awaits.
        """
        return [
            Message(
                payload=format_alert(
                    new,
                    booking_url=self._cfg.booking_url,
                    mention_everyone=self._cfg.mention_everyone,
                ),
                covers=tuple(slot.key for slot in new),
            )
        ]

    def heartbeat_extras(self) -> HeartbeatExtras:
        """Nothing to add. Jeffco reports unrecognised school names here, and
        replaces the footer with what to do about them."""
        return HeartbeatExtras()
```

`apps/melanzana/src/melanzana/main.py`:

```python
"""Wire a MelanzanaMonitor to the shared runner."""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

from melanzana.config import LOG_PREFIX, load_config
from melanzana.monitor import MelanzanaMonitor
from monitor.config import make_log
from monitor.discord import post
from monitor.runner import install_shutdown_handlers, run_forever
from monitor.timing import system_now
from monitor.types import Payload

#: Generous but finite. Without a timeout a hung Cowlendar connection stalls the
#: loop indefinitely, which looks identical to "nothing new".
HTTP_TIMEOUT_SEC = 20.0

log = make_log(LOG_PREFIX)


async def _main() -> None:
    cfg = load_config(os.environ)
    log(
        f"app config — window={cfg.window_days}d tz={cfg.timezone} "
        f"mentionEveryone={cfg.mention_everyone} calendar={cfg.calendar_id}"
    )

    # This process's owner installs its own signal handlers; the library is a loop,
    # not a supervisor.
    install_shutdown_handlers(log)

    async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT_SEC)) as client:
        # ONE clock, passed to both. The monitor needs it for its window arithmetic
        # and the runner for its health timestamps; two independent readings are
        # harmless in production and quietly confusing in a test.
        monitor = MelanzanaMonitor(cfg, client=client, now_unix=system_now)

        async def poster(url: str, payload: Payload) -> None:
            await post(url, payload, client)

        await run_forever(monitor, cfg.runner, poster=poster, now_unix=system_now, log=log)


def main() -> None:
    try:
        asyncio.run(_main())
    except SystemExit:
        raise
    except BaseException as err:
        # The message, not the exception object: a repr can carry a URL, and a
        # webhook's path is a credential.
        print(f"[{LOG_PREFIX}] fatal: {err}", file=sys.stderr, flush=True)
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
```

`apps/melanzana/.env.example`:

```
# Required — create a webhook in your Discord channel: Channel Settings → Integrations → Webhooks
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxx/yyyy

# Optional — defaults shown. Override only if needed.
# CALENDAR_ID=685b42f202405a8372cd6b78
# VARIANT_ID=41855678382123
# POLL_INTERVAL_SEC=10
# POLL_JITTER_PCT=20
# WINDOW_DAYS=60
# TIMEZONE=America/Denver
# BOOKING_URL=https://melanzana.com/pages/how-to-shop
# MENTION_EVERYONE=false
# STATE_PATH=/data/state.json

# --- ops (all optional) ---
# Separate channel for heartbeat/death/recovery. Falls back to DISCORD_WEBHOOK_URL.
# STATUS_WEBHOOK_URL=https://discord.com/api/webhooks/xxxx/zzzz
# Heartbeat cadence in seconds (default 86400 = daily). Ignored when HEARTBEAT_AT is set.
# HEARTBEAT_INTERVAL_SEC=86400
# Heartbeat at a fixed America/Denver wall-clock time instead of an interval.
# HEARTBEAT_AT=07:00
# No successful poll for this many seconds → death alert (default 600).
# STALL_ALERT_SEC=600
```

- [ ] **Step 5: Run everything to verify it passes**

Run: `uv run pytest -q && uv run mypy --strict lib apps tests && uv run ruff check .`
Expected: 4 + 5 passed in the new files, 125 total, mypy `Success`, ruff clean.

`mypy --strict` is also the only thing that checks `MelanzanaMonitor` actually satisfies `Monitor[Slot]`, since the protocol is structural. Make that explicit rather than incidental — add this to `monitor.py` so a signature drift fails type-checking instead of failing at runtime:

```python
# At the bottom of monitor.py. Add `from typing import TYPE_CHECKING` and
# `from monitor.types import Monitor` to the imports.
if TYPE_CHECKING:  # a compile-time assertion, no runtime cost, no fake arguments
    def _assert_satisfies_protocol(m: MelanzanaMonitor) -> Monitor[Slot]:
        return m
```

- [ ] **Step 6: Prove the app starts and refuses bad config**

```bash
# Missing webhook — must fail fast with a useful message and no traceback noise.
uv run python -c "
from melanzana.config import load_config
try:
    load_config({})
except Exception as err:
    print('refused:', err)
"

# A real start against a temp state path. It will fail to reach Discord, which is
# fine — what matters is the two startup lines and that it polls.
DISCORD_WEBHOOK_URL=https://discord.test/webhook STATE_PATH=/tmp/melz-smoke.json \
  timeout 25 uv run python -m melanzana.main; echo "exit=$?"
```

Expected: `refused: Config error: DISCORD_WEBHOOK_URL is required`, then two `[melanzana-monitor]` startup lines, a real Cowlendar poll, `firstRun=true`, and `/tmp/melz-smoke.json` written with the current slot keys. `exit=124` is the timeout doing its job.

- [ ] **Step 7: Commit**

```bash
git add apps/melanzana tests/melanzana/test_config.py tests/melanzana/test_monitor.py
git commit -m "Wire melanzana's config, Monitor implementation, and entrypoint"
```

---

### Task 13: the differential harness

The check worth trusting. It compares the only output a user sees, and catches what unit tests miss: field ordering, number formatting, the `@everyone` and `allowed_mentions` pair, the em-dash.

Three facts established during planning, so none of them needs rediscovering:
- Node's `JSON.stringify(canon(x), null, 2)` and Python's `json.dumps(x, sort_keys=True, indent=2, ensure_ascii=False)` are byte-identical on a realistic payload — emoji, em-dash, `@everyone` pair, 577 bytes both sides.
- `localeCompare` and Python's code-point ordering agree on every realistic key pair, including `"weird-key"` vs `"2026-12-01"` and `"a-b"` vs `"ab"`, so `sorted()` is a faithful translation of the TypeScript's day sort. Case 5 pins that rather than trusting it.
- The fixture alone only exercises a single day, so it cannot express day grouping, ordering, the `MAX_DAY_CARDS` overflow note, the unparseable-key fallback, or the mixed-key sort. The harness therefore runs the fixture case **plus** six synthetic cases built from identical literals on both sides. The fixture case is still first and still mandatory.

**Files:**
- Create: `tools/dump_payloads.py`
- Create: `~/personal/melanzana-monitor/tools/dump-payloads.ts` (the other repo — a new file that changes no behaviour)
- Create: `tools/parity-diff.sh`

**Interfaces:**
- Consumes: `melanzana.alert.format_alert`, `melanzana.cowlendar.parse_availability`, `melanzana.detector.filter_bookable`, `monitor.discord` (`format_heartbeat`, `format_status_alert`), `monitor.health.init_health`, `melanzana.config.LABELS`.
- Produces: `tools/parity-diff.sh` exiting 0 only when both `MENTION_EVERYONE` states diff clean.

- [ ] **Step 1: Write the Python dump script**

`tools/dump_payloads.py`:

```python
"""Dump every Discord payload melanzana can produce, as canonical JSON.

Half of the differential harness. The TypeScript half lives at
`melanzana-monitor/tools/dump-payloads.ts` and must print byte-identical output.

Both sides pin the clock and build the synthetic cases from the same literals, so
any difference in the output is a difference in the *implementations*. Verified
during planning: Node's JSON.stringify(canon(x), null, 2) and this
json.dumps(..., sort_keys=True, indent=2, ensure_ascii=False) agree byte for byte.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from melanzana.alert import format_alert
from melanzana.config import LABELS
from melanzana.cowlendar import parse_availability
from melanzana.detector import filter_bookable
from melanzana.types import Slot
from monitor.discord import format_heartbeat, format_status_alert
from monitor.health import init_health

#: Frozen clock. Month selection and weekday rendering must be deterministic rather
#: than passing until the first of the month.
NOW = 1796000000
WINDOW_DAYS = 60
BOOKING_URL = "https://melanzana.com/pages/how-to-shop"
FIXTURE = Path(__file__).resolve().parents[1] / "tests/melanzana/fixtures/availability-sample.json"


def _slot(key: str, qty_left: int) -> Slot:
    # start_unix is inside the window for every synthetic case; these cases exercise
    # rendering, and the window cut has its own tests.
    return Slot(key=key, start_unix=1796146200, qty_left=qty_left, is_bookable=True)


def build(mention: bool) -> dict[str, Any]:
    fixture_slots = filter_bookable(
        parse_availability(json.loads(FIXTURE.read_text(encoding="utf-8"))), NOW, WINDOW_DAYS
    )

    multi_day = [
        _slot("2026-12-02 09:00", 5),
        _slot("2026-12-01 11:00", 2),
        _slot("2026-12-01 10:30", 4),
    ]
    overflow = [_slot(f"2026-09-{day:02d} 10:00", 4) for day in range(1, 31)]
    unparseable = [_slot("weird-key", 4)]
    # A parseable key and a raw one in the same batch — the only place `localeCompare`
    # and Python's code-point `sorted` could disagree, since ICU can treat punctuation
    # as ignorable. Checked against node during planning and they agree on every
    # realistic pair, including "weird-key" vs "2026-12-01" and "a-b" vs "ab"; this
    # case exists so the assumption is pinned rather than remembered.
    mixed = [_slot("weird-key", 1), _slot("2026-12-01 10:30", 4), _slot("also-weird", 2)]

    heartbeat_state = replace(init_health(1000), items_tracked=7, last_success_unix=1500)
    death_state = replace(init_health(1000), last_success_unix=1000, consecutive_failures=4)

    return {
        "1-fixture": format_alert(
            fixture_slots, booking_url=BOOKING_URL, mention_everyone=mention
        ).to_dict(),
        "2-multi-day": format_alert(
            multi_day, booking_url=BOOKING_URL, mention_everyone=mention
        ).to_dict(),
        "3-overflow": format_alert(
            overflow, booking_url=BOOKING_URL, mention_everyone=mention
        ).to_dict(),
        "4-unparseable-key": format_alert(
            unparseable, booking_url=BOOKING_URL, mention_everyone=mention
        ).to_dict(),
        "5-mixed-keys": format_alert(
            mixed, booking_url=BOOKING_URL, mention_everyone=mention
        ).to_dict(),
        "6-heartbeat": format_heartbeat(LABELS, heartbeat_state, 1000 + 3600).to_dict(),
        "7-death": format_status_alert("death", LABELS, death_state, 2000).to_dict(),
        "8-recovery": format_status_alert("recovery", LABELS, init_health(1000), 2100).to_dict(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mention", choices=("true", "false"), required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.mention == "true"), sort_keys=True, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the TypeScript dump script in the other repo**

`~/personal/melanzana-monitor/tools/dump-payloads.ts`. A new file that imports the existing modules and changes nothing:

```ts
/**
 * Dump every Discord payload melanzana can produce, as canonical JSON.
 *
 * Half of the differential harness; the Python half lives at
 * `monitors/tools/dump_payloads.py` and must print byte-identical output. Read-only
 * with respect to this repo's behaviour: it imports src/ and prints.
 *
 *   npx tsx tools/dump-payloads.ts --mention true
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { formatAlert, formatHeartbeat, formatStatusAlert } from '../src/discord';
import { filterBookable } from '../src/detector';
import { parseAvailability } from '../src/cowlendar';
import { initHealth } from '../src/health';
import type { Config, Slot } from '../src/types';

const NOW = 1796000000;
const WINDOW_DAYS = 60;
const BOOKING_URL = 'https://melanzana.com/pages/how-to-shop';

const mentionArg = process.argv[process.argv.indexOf('--mention') + 1];
if (mentionArg !== 'true' && mentionArg !== 'false') {
  throw new Error('usage: dump-payloads.ts --mention true|false');
}
const mentionEveryone = mentionArg === 'true';

const cfg: Config = {
  discordWebhookUrl: 'https://discord.test/webhook',
  calendarId: 'CAL123',
  variantId: 'VAR456',
  pollIntervalSec: 10,
  pollJitterPct: 20,
  windowDays: WINDOW_DAYS,
  timezone: 'America/Denver',
  bookingUrl: BOOKING_URL,
  mentionEveryone,
  statePath: '/data/state.json',
  heartbeatIntervalSec: 86400,
  stallAlertSec: 600,
};

const slot = (key: string, qtyLeft: number): Slot => ({
  key,
  startUnix: 1796146200,
  qtyLeft,
  isBookable: true,
});

const fixture = JSON.parse(
  readFileSync(fileURLToPath(new URL('../test/fixtures/availability-sample.json', import.meta.url)), 'utf8'),
);
const fixtureSlots = filterBookable(parseAvailability(fixture), NOW, WINDOW_DAYS);

const multiDay = [slot('2026-12-02 09:00', 5), slot('2026-12-01 11:00', 2), slot('2026-12-01 10:30', 4)];
const overflow = Array.from({ length: 30 }, (_, i) =>
  slot(`2026-09-${String(i + 1).padStart(2, '0')} 10:00`, 4),
);
const unparseable = [slot('weird-key', 4)];
// Mixed parseable/raw keys: the only place localeCompare and a code-point sort could
// diverge. Same literals as the Python side, in the same order.
const mixed = [slot('weird-key', 1), slot('2026-12-01 10:30', 4), slot('also-weird', 2)];

const heartbeatState = { ...initHealth(1000), slotsTracked: 7, lastSuccessUnix: 1500 };
const deathState = { ...initHealth(1000), lastSuccessUnix: 1000, consecutiveFailures: 4 };

const out = {
  '1-fixture': formatAlert(fixtureSlots, cfg),
  '2-multi-day': formatAlert(multiDay, cfg),
  '3-overflow': formatAlert(overflow, cfg),
  '4-unparseable-key': formatAlert(unparseable, cfg),
  '5-mixed-keys': formatAlert(mixed, cfg),
  '6-heartbeat': formatHeartbeat(heartbeatState, 1000 + 3600),
  '7-death': formatStatusAlert('death', deathState, 2000),
  '8-recovery': formatStatusAlert('recovery', initHealth(1000), 2100),
};

// Sorted keys, so neither side's object-literal ordering can mask a difference.
// JSON.stringify already drops undefined-valued properties, which is the behaviour
// the Python Payload.to_dict() mirrors by omitting absent keys.
function canon(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canon);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value as Record<string, unknown>)
        .sort()
        .map((k) => [k, canon((value as Record<string, unknown>)[k])]),
    );
  }
  return value;
}

console.log(JSON.stringify(canon(out), null, 2));
```

- [ ] **Step 3: Write the diff runner**

`tools/parity-diff.sh`:

```bash
#!/usr/bin/env bash
#
# The differential harness: both implementations render every payload on a frozen
# clock, and the diff must be empty. This compares the only output a user sees.
#
# Also cmps the fixture, because "copied byte-for-byte" is a claim worth checking
# mechanically rather than trusting.
set -euo pipefail
cd "$(dirname "$0")/.."

TS_REPO="${TS_REPO:-$HOME/personal/melanzana-monitor}"
OUT="${TMPDIR:-/tmp}/melz-parity"
mkdir -p "$OUT"

echo "==> fixture is byte-identical"
cmp "$TS_REPO/test/fixtures/availability-sample.json" \
    tests/melanzana/fixtures/availability-sample.json
echo "    ok ($(wc -c < tests/melanzana/fixtures/availability-sample.json | tr -d ' ') bytes)"

for mention in true false; do
  echo "==> MENTION_EVERYONE=$mention"
  (cd "$TS_REPO" && npx --yes tsx tools/dump-payloads.ts --mention "$mention") > "$OUT/ts-$mention.json"
  uv run python tools/dump_payloads.py --mention "$mention" > "$OUT/py-$mention.json"
  # -u so a real difference is readable rather than just reported.
  diff -u "$OUT/ts-$mention.json" "$OUT/py-$mention.json"
  echo "    identical ($(wc -c < "$OUT/py-$mention.json" | tr -d ' ') bytes)"
done

echo
echo "parity: TypeScript and Python payloads are identical"
```

```bash
chmod +x tools/parity-diff.sh
```

- [ ] **Step 4: Run the harness**

```bash
./tools/parity-diff.sh
```

Expected: `identical` for both mention states and `parity: TypeScript and Python payloads are identical`.

If it differs, the diff *is* the bug report. The likely culprits, in order: a key present on one side and absent on the other (`Payload.to_dict()` omission rules), day or line ordering (`sorted` vs `localeCompare`), or an integer rendered as `4.0` (an `int` that became a `float` somewhere in the qty path).

This is deliberately **not** a pytest test: it needs node and the sibling TypeScript repo, and a suite that fails when a neighbouring directory moves is a suite people learn to ignore. It is a gate run by hand and recorded in the README.

- [ ] **Step 5: Commit both repos**

```bash
git add tools
git commit -m "Add the differential payload harness"

cd ~/personal/melanzana-monitor
git add tools/dump-payloads.ts
git commit -m "Add a payload dump script for the Python port's parity harness"
cd ~/personal/monitors
```

---

### Task 14: the container image

One gotcha dominates this task: the host's startup script does `chown 1000:1000 /var/lib/<app>-data`, and its comment says "Containers run as non-root uid 1000 (USER node)". `python:3.13-slim` ships **no** uid-1000 user, so the image must create one or `/data` is unwritable and every tick logs a state-write failure while alerting normally — divergence #2 turns that from a crash into a quiet degradation, which is worse to diagnose.

**Files:**
- Create: `Dockerfile` (repo root — **one recipe for every app**, not one per app)
- Modify: `.dockerignore` (created in Task 1 — verify it excludes `tests`, `tools`, `infra`, `docs`, and `**/.env`)

**Why one Dockerfile rather than `apps/<app>/Dockerfile`:** the uid-1000 `useradd` below is a line that, if omitted, produces a monitor which polls and alerts perfectly while never persisting its baseline — and divergence #2 makes that a log line rather than a crash. Putting it in a file each new app copies makes forgetting it the default outcome. One recipe, selected by `--build-arg APP=`, means app #3 cannot forget.

- [ ] **Step 1: Write the Dockerfile**

`Dockerfile` at the repo root:

```dockerfile
# One image recipe for every app in the workspace.
#
#   docker build --platform linux/amd64 --build-arg APP=melanzana -t <tag> .
#
# Deliberately NOT one Dockerfile per app: the uid-1000 user below is required by
# the host's volume ownership, and a per-app copy makes omitting it easy and its
# absence quiet.
FROM python:3.13-slim AS build
ARG APP

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv

# COMPILE_BYTECODE so the container does not pay import compilation on every
# restart; LINK_MODE=copy because the cache and the venv are on different layers.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Manifests first, then sources: a code change must not re-resolve dependencies.
# Every member's manifest is copied because the workspace root declares them all
# and uv resolves the whole graph even when installing one package.
COPY pyproject.toml uv.lock ./
COPY lib/monitor/pyproject.toml lib/monitor/
COPY apps/ apps/
RUN find apps -mindepth 2 -not -name pyproject.toml -not -type d -delete || true
RUN uv sync --frozen --no-dev --package "$APP" --no-install-workspace

COPY lib/monitor lib/monitor
COPY apps/ apps/
RUN uv sync --frozen --no-dev --package "$APP"


FROM python:3.13-slim
ARG APP

# uid 1000 to match the host volume. The startup script chowns
# /var/lib/<app>-data to 1000:1000 — it was written for the node image's built-in
# uid-1000 user, and python:3.13-slim has no such user. Without this, /data is
# unwritable: the monitor still polls and alerts, but every tick logs a state-write
# failure and a restart silently re-baselines everything currently open.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data \
 && chown 1000:1000 /data

COPY --from=build --chown=1000:1000 /app /app

# PYTHONUNBUFFERED so stdout reaches `docker logs` and the Cloud Logging agent as
# it happens. Without it a crashed container can lose its last words to a buffer.
# APP is promoted to ENV so the CMD below can expand it at runtime.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP=${APP}

USER 1000
VOLUME ["/data"]

# `exec` so python replaces the shell as PID 1 and receives SIGTERM directly —
# without it systemd's stop signal goes to /bin/sh and the app is killed instead of
# shut down. Shell form is required because $APP must expand at runtime.
# No EXPOSE and no health port: the health server is not ported.
CMD ["sh", "-c", "exec python -m $APP.main"]
```

The `find … -delete` line looks odd and earns its place: it strips app sources from the manifest-only layer so that editing melanzana's code does not invalidate the dependency layer, while still giving uv every member's `pyproject.toml`.

- [ ] **Step 2: Build for the host's architecture**

The `e2-micro` is x86; a native arm64 image fails on the host with "exec format error". Docker Desktop must be running — it was not, during planning.

```bash
docker info >/dev/null 2>&1 || open -a Docker   # then wait for the daemon
cd ~/personal/monitors
docker build --platform linux/amd64 --build-arg APP=melanzana -t melanzana-monitor:v2.0.0 .
```

Expected: a successful build. Then check the footprint claim the spec asked to confirm:

```bash
docker image inspect melanzana-monitor:v2.0.0 --format '{{.Size}}' | awk '{print $1/1024/1024 " MB image"}'
docker image inspect melanzana-monitor:v2.0.0 --format '{{.Architecture}}'
```

Expected: `amd64`. Image size is not the 35 MB figure — that was resident memory, measured in Task 16.

- [ ] **Step 3: Smoke-test the container**

```bash
mkdir -p /tmp/melz-data && chmod 777 /tmp/melz-data
docker run --rm --platform linux/amd64 \
  --memory=128m \
  -e DISCORD_WEBHOOK_URL=https://discord.test/webhook \
  -e STATE_PATH=/data/state.json \
  -v /tmp/melz-data:/data \
  --name melanzana-smoke melanzana-monitor:v2.0.0 &
sleep 30
docker stats --no-stream melanzana-smoke || true
docker logs melanzana-smoke
cat /tmp/melz-data/state.json
docker stop melanzana-smoke 2>/dev/null || true
```

Expected: the two `[melanzana-monitor]` startup lines appear **immediately** (proving `PYTHONUNBUFFERED`), a real Cowlendar poll runs, `state.json` is written as uid 1000 with the current slot keys, `docker stats` shows resident memory (the number to compare against Node's 104 MB), and the status posts fail against the fake webhook without disturbing the loop — which is divergence #1 visible in production form.

Record the `docker stats` memory figure; Task 16 reports it against the spec's ~35 MB expectation.

Also confirm the two traps this task exists to close:

```bash
# /data is writable by the runtime user, not just root.
docker run --rm --platform linux/amd64 --entrypoint sh melanzana-monitor:v2.0.0 \
  -c 'id -u; touch /data/probe && echo "/data writable" || echo "/data NOT WRITABLE"'

# No .env rode into the image.
docker run --rm --platform linux/amd64 --entrypoint sh melanzana-monitor:v2.0.0 \
  -c 'find /app -name ".env" | grep . && echo "SECRET LEAKED INTO IMAGE" || echo "no .env in image"'
```

Expected: `1000`, `/data writable`, `no .env in image`.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "Add one container image recipe for every app"
```

---

### Task 15: infra wiring, the deploy.sh fix, and docs

Four small infra edits and one real bug fix. `jeffco`'s entry is not touched.

**Files:**
- Modify: `infra/apps.auto.tfvars` (melanzana's `image_tag`)
- Modify: `infra/variables.tf` (add `melanzana_heartbeat_at`)
- Modify: `infra/main.tf` (add `HEARTBEAT_AT` to melanzana's env)
- Modify: `infra/deploy.sh` (the unbound `$INSTANCE`, and the swallowed remote exit status)
- Modify: `infra/templates/startup.sh.tftpl` (one app's missing image aborts every later app)
- Create: `apps/README.md` (the add-an-app checklist)
- Modify: `README.md`, `infra/README.md`

- [ ] **Step 1: Fix the deploy script**

`infra/deploy.sh:86` reads `echo "==> re-running startup script on $INSTANCE"`. `INSTANCE` is never assigned, and the script runs under `set -euo pipefail`, so this aborts **after** `terraform apply` and **before** `google_metadata_script_runner startup` — producing exactly the half-deploy the script exists to prevent. Verified: `INSTANCE: unbound variable`, exit 1.

```bash
# infra/deploy.sh:86
-echo "==> re-running startup script on $INSTANCE"
+echo "==> re-running startup script on $(tf_out instance_name monitors)"
```

Confirm the whole script parses and that no other unbound variable is waiting:

```bash
bash -n infra/deploy.sh && grep -n '\$[A-Z_]\{3,\}' infra/deploy.sh
```

Expected: no syntax errors, and every remaining uppercase reference is one the script assigns or a `tf_out` call.

- [ ] **Step 2: Stop the deploy gate from reporting success on a failed startup script**

Two defects, one failure. Both are in live code and both were verified during planning.

**(a) `deploy.sh:87` throws away the remote exit status.** `deploy.sh` sets `-o pipefail`, but the pipeline runs in the *remote* shell, which does not. So `sudo google_metadata_script_runner startup 2>&1 | tail -25` returns `tail`'s status — measured as **0** on a command that exited 1. The half of the deploy that Terraform cannot do can fail while the script proceeds to `verify`, and `verify` shows the *previous* container still healthily running the *previous* image. That is precisely the deploy-that-looks-like-it-worked this script exists to prevent.

```bash
# infra/deploy.sh:87
-on_host 'sudo google_metadata_script_runner startup 2>&1 | tail -25'
+# `set -o pipefail` on the remote side too: without it this pipeline reports tail's
+# exit status, so a failed startup script looks like a successful deploy.
+on_host 'set -o pipefail; sudo google_metadata_script_runner startup 2>&1 | tail -25'
```

**(b) `startup.sh.tftpl:44-47` aborts every app after the failing one.** The `%{ for }` is a *template* loop: it unrolls into sequential bash, so `exit 1` leaves the script. Terraform iterates a map in lexicographic key order, so with `jeffco` and `melanzana` today a jeffco image failure means melanzana never gets its env file, its unit file, `daemon-reload`, or its restart. Adding an app named `fashionjobs` puts a *new, unpushed* app first and makes that the common case.

```hcl
 if ! docker image inspect ${image} >/dev/null 2>&1; then
   echo "${name}: image unavailable; leaving the running container alone" >&2
-  exit 1
+  # Record and continue. `exit` here would be a template-loop `exit` — it leaves the
+  # whole script, so every app sorting after this one silently keeps its old env
+  # file, its old unit, and no restart. Terraform iterates the app map in
+  # lexicographic order, so a new app's first deploy is exactly when that bites.
+  failed="$failed ${name}"
+  continue_to_next_app=1
+else
+  continue_to_next_app=0
+fi
+
+if [[ $continue_to_next_app -eq 0 ]]; then
```

with the per-app body indented into that `if`, `failed=""` initialised next to `restart_list=""`, and at the very end, *after* the restart loop:

```bash
if [[ -n "$failed" ]]; then
  echo "monitors: FAILED to deploy:$failed (other apps were left running)" >&2
  exit 1
fi
echo "monitors: startup complete"
```

The ordering matters: every healthy app is configured and restarted first, and the non-zero exit comes last so the deploy still reports the failure — now that (a) lets it through.

- [ ] **Step 3: Verify the two fixes without touching the host**

```bash
cd infra
terraform fmt -check
# Render the startup script and shellcheck-parse it, rather than finding out on boot.
terraform console <<<'templatefile("${path.module}/templates/startup.sh.tftpl", {region="us-west1", images={a="img-a", b="img-b"}, env_b64={a="Zm9v", b="Zm9v"}, units_b64={a="Zm9v", b="Zm9v"}})' \
  | sed -e 's/^"//' -e 's/"$//' -e 's/\\n/\n/g' -e 's/\\"/"/g' > /tmp/startup-rendered.sh
bash -n /tmp/startup-rendered.sh && echo "renders and parses"
grep -c 'image unavailable' /tmp/startup-rendered.sh   # expect 2 — once per app
cd ..
```

Expected: `renders and parses`, and `2`. The point of rendering with two apps is that the bug only exists in the unrolled form.

- [ ] **Step 4: Wire HEARTBEAT_AT**

The spec: "It is wired into Terraform only once the library reads it; adding the variable sooner would be config that silently does nothing." The library now reads it, so it is wired — for melanzana only. Jeffco is still TypeScript and does not read `HEARTBEAT_AT`; setting it there would be exactly the config-that-does-nothing the spec warns about.

`infra/variables.tf`, after the `melanzana_mention_everyone` block:

```hcl
# 07:00 America/Denver — early enough to read over coffee and still ahead of most
# of the day's job postings, so a missing heartbeat can be acted on before the
# window that matters. Empty falls back to HEARTBEAT_INTERVAL_SEC, which anchors
# the heartbeat to process start and therefore reports at whatever time the last
# deploy happened.
#
# Only melanzana. jeffco is still TypeScript and does not read this name, so
# setting it there would be config that silently does nothing.
variable "melanzana_heartbeat_at" {
  type        = string
  description = "HH:MM America/Denver time for melanzana's daily heartbeat. Empty = use HEARTBEAT_INTERVAL_SEC."
  default     = "07:00"

  validation {
    condition     = var.melanzana_heartbeat_at == "" || can(regex("^([01][0-9]|2[0-3]):[0-5][0-9]$", var.melanzana_heartbeat_at))
    error_message = "melanzana_heartbeat_at must be empty or an HH:MM 24-hour time."
  }
}
```

`infra/main.tf`, inside `local.app_env.melanzana`'s `merge()` — as a second conditional map, matching how `STATUS_WEBHOOK_URL` is already handled:

```hcl
    melanzana = merge(
      {
        DISCORD_WEBHOOK_URL    = var.melanzana_discord_webhook_url
        STATE_PATH             = "/data/state.json"
        WINDOW_DAYS            = tostring(var.melanzana_window_days)
        MENTION_EVERYONE       = tostring(var.melanzana_mention_everyone)
        HEARTBEAT_INTERVAL_SEC = tostring(var.heartbeat_interval_sec)
        STALL_ALERT_SEC        = tostring(var.stall_alert_sec)
      },
      var.melanzana_status_webhook_url != "" ? { STATUS_WEBHOOK_URL = var.melanzana_status_webhook_url } : {},
      var.melanzana_heartbeat_at != "" ? { HEARTBEAT_AT = var.melanzana_heartbeat_at } : {},
    )
```

- [ ] **Step 5: Bump the tag**

`infra/apps.auto.tfvars` — melanzana only:

```hcl
  melanzana = {
    image     = "melanzana-monitor"
    image_tag = "v2.0.0"
    memory    = "128m"
  }
```

The image *name* does not change: the registry repository is `melanzana` and the image inside it is `melanzana-monitor`, and renaming it would mean a new repository for no benefit. `v2.0.0` because the implementation language changed under a stable contract. `128m` stays — a leak backstop, not a tuning knob, and Task 16 confirms the headroom.

- [ ] **Step 6: Validate the Terraform without applying**

```bash
cd infra
terraform fmt -check
terraform validate
./deploy.sh --plan
cd ..
```

Expected: `Success! The configuration is valid.` and a plan with **one** change — `google_compute_instance.host` updated in place, because the startup script metadata now carries `HEARTBEAT_AT` and the new image tag. **No delete, no replace.** If the plan proposes replacing the instance, stop: that destroys the boot disk and every app's `state.json`. `deploy.sh` refuses such a plan, but read it yourself first.

- [ ] **Step 7: Update the docs**

`README.md` — replace the Status section and add the local-development and parity notes:

````markdown
## Status

`infra/` is live: it is the Terraform root for the host, which runs in the
**`cobs-cloud`** project as an instance named `monitors`. Both apps run there as
systemd units.

`lib/monitor` and `apps/melanzana` are implemented in Python. `jeffco` still runs
the TypeScript image built in its own repo; its port has its own design cycle.

## Working on it

```bash
uv sync
uv run pytest -q
uv run mypy --strict lib apps tests tools
uv run ruff check .
./tools/parity-diff.sh          # needs node and ~/personal/melanzana-monitor
```

`parity-diff.sh` is the check worth trusting: both the TypeScript and Python
implementations render every Discord payload on a frozen clock and the diff must be
empty. It is not part of `pytest` because it needs the sibling repo.

## Deliberate divergences from the TypeScript melanzana

1. A failed Discord post no longer stops the tick. The runner withholds the
   affected message's keys from the baseline, banks the rest, keeps polling, and
   counts the tick as healthy. A duplicate costs one glance; a swallowed slot can
   cost a day's work.
2. A failed state write is logged and the loop continues on the in-memory
   baseline, rather than being reported as a poll failure. Reporting it as one
   would throttle polling, latch a false death alert, and suppress the heartbeat —
   all while alerts were arriving normally.

`HEARTBEAT_AT` is new: unset it behaves exactly as before, set to `HH:MM` the
heartbeat lands at that America/Denver wall-clock time instead of drifting with
process start. Production runs `07:00`.
````

`apps/README.md` — new, and the highest-value thing here for app #3. The name of an app appears in roughly fifteen places across eight files, most of it irreducible (Terraform plus Python packaging), so the answer is a checklist in the right order rather than a framework:

````markdown
# Adding an app

The library owns the loop. An app owns its source, its key, its alert rendering,
its filtering, and its authentication. Four methods — see
`lib/monitor/src/monitor/types.py`, the `Monitor` protocol.

**Order matters.** The registry repository is created by Terraform from the app
list, so the tfvars entry comes before the image push, and the push comes before
the deploy — otherwise the startup script finds no image.

1. `apps/<app>/pyproject.toml` — copy melanzana's. Name it `<app>`; depend on
   `monitor` (`{ workspace = true }`) plus whatever HTTP client the source needs.
2. Root `pyproject.toml` — add `apps/<app>` to `[tool.uv.workspace] members`.
3. `uv sync` — regenerates `uv.lock`. The Dockerfile needs the lock committed.
4. `apps/<app>/src/<app>/` — `types.py`, the source client, `alert.py`,
   `config.py`, `monitor.py`, `main.py`. Copy melanzana's `main.py` and change the
   two constructor calls; it is the one file that is meant to be per-app, because
   it is where the HTTP client is chosen.
5. `tests/<app>/` — the source client, the renderer, the config, the four methods.
6. `infra/variables.tf` — one `sensitive = true` variable per credential, plus
   `<app>_heartbeat_at` if it should report at a fixed hour.
7. `infra/main.tf` — a `local.app_env.<app>` map. The instance has a precondition
   that fails the plan if this is missing, so a forgotten entry cannot ship.
8. `infra/apps.auto.tfvars` — the `apps` entry: `image`, `image_tag`, `memory`.
9. `infra/terraform.tfvars` — the secrets (gitignored, never committed).
10. `cd infra && terraform apply -target=google_artifact_registry_repository.app`
    — creates the repository so there is somewhere to push.
11. Build and push. **`--platform linux/amd64`**: the host is x86 and an arm64
    image fails with "exec format error".
    `docker build --platform linux/amd64 --build-arg APP=<app> -t "$REGION-docker.pkg.dev/$PROJECT/<app>/<image>:<tag>" .`
12. `./infra/deploy.sh` — applies, re-runs the startup script, verifies. Never a
    bare `terraform apply`.
13. Check the first log lines: `./infra/logs.sh <app> --freshness=10m`.

## The three things that fail quietly

- **`firstRun=true` when you expected `false`.** The app did not read an existing
  baseline, so everything currently open goes unannounced. Nothing looks wrong.
- **A state-write failure.** `/data` unwritable logs one line per tick while
  alerts keep arriving. The root `Dockerfile` handles the uid-1000 cause; if you
  wrote your own image, that is the first place to look.
- **A missing `HEARTBEAT_AT`.** The heartbeat silently reverts to anchoring on
  process start, so it arrives at whatever time the last deploy happened.

## What you do NOT write

State persistence, the key-set diff, first-run suppression, jitter, backoff,
`SourceBusy` cadence handling, health folding, heartbeat and stall/recovery
messages, the status-webhook fallback, inter-message spacing, post-failure
withholding, or Discord transport. If you are writing one of those, check whether
`lib/monitor` already has it.
````

`infra/README.md` — add to the "Supervision" consequences list:

```markdown
- **Python images must create a uid-1000 user.** The startup script chowns
  `/var/lib/<app>-data` to `1000:1000`, which was written for the node image's
  built-in `node` user. `python:3.13-slim` has no uid-1000 user, so an image
  without one gets an unwritable `/data` — and the library's state-write handling
  turns that into a per-tick log line rather than a crash, so it is quiet.
```

And in the deploying section, note the env var:

```markdown
`HEARTBEAT_AT` (melanzana only, default `07:00`) fixes the heartbeat to an
America/Denver wall-clock hour. jeffco does not read it while it is TypeScript, so
it is deliberately not set there.
```

- [ ] **Step 8: Commit**

Three commits, because the two defect fixes stand on their own and should be
revertable without touching the deploy:

```bash
git add infra/deploy.sh infra/templates/startup.sh.tftpl
git commit -m "Stop a failed startup script from reporting a successful deploy"

git add apps/README.md README.md infra/README.md
git commit -m "Document adding an app and the port's divergences"

git add infra/variables.tf infra/main.tf infra/apps.auto.tfvars
git commit -m "Wire HEARTBEAT_AT and bump melanzana to v2.0.0"
```

---

### Task 16: deploy, verify, and rehearse the rollback

The host is live and healthy. Everything here is reversible, and the rollback is rehearsed rather than assumed — the spec asks for it explicitly, before jeffco's unit is ever touched.

**Files:** none. This task runs commands and records what they say.

- [ ] **Step 1: Back up the Terraform state before touching it**

State is a local file holding every webhook and the SFE PIN in plaintext. Losing it costs melanzana's baseline, which would re-alert its entire backlog to a real channel.

```bash
cp infra/terraform.tfstate ~/Documents/monitors-tfstate-backup-2026-08-20.json
ls -la ~/Documents/monitors-tfstate-backup-2026-08-20.json
```

Expected: a file outside the repo, non-zero size.

- [ ] **Step 2: Push the image**

```bash
REGION=us-west1
PROJECT=cobs-cloud
gcloud auth configure-docker "$REGION-docker.pkg.dev"

docker tag melanzana-monitor:v2.0.0 \
  "$REGION-docker.pkg.dev/$PROJECT/melanzana/melanzana-monitor:v2.0.0"
docker push "$REGION-docker.pkg.dev/$PROJECT/melanzana/melanzana-monitor:v2.0.0"

# Prove both tags exist — the rollback in step 5 depends on v1.2.0 still being there.
gcloud artifacts docker tags list \
  "$REGION-docker.pkg.dev/$PROJECT/melanzana/melanzana-monitor" --project "$PROJECT"
```

Expected: `v2.0.0` and `v1.2.0` both listed. If `v1.2.0` is gone there is nothing to roll back to — stop and rebuild it from the TypeScript repo before deploying.

- [ ] **Step 3: Deploy**

```bash
./infra/deploy.sh
```

Expected, in order: a plan with no deletes, `terraform apply` succeeding, `melanzana: changed, will restart` and `jeffco: unchanged` from the startup script, then the verify block showing both units active, both containers up, and recent output per app. `jeffco: unchanged` is the line that proves the other app was not disturbed.

If the startup script prints `melanzana: image unavailable; leaving the running container alone`, the push did not land — the old container is still running and nothing is broken. Fix the push and re-run.

- [ ] **Step 4: Verify the deploy against what the spec predicted**

```bash
./infra/logs.sh melanzana --freshness=10m
./infra/deploy.sh --verify
```

Check and record:
- Both `[melanzana-monitor]` startup lines, with `heartbeatAt=07:00` and `statusChannel=main`.
- `firstRun=false` — the existing `/var/lib/melanzana-data/state.json` was read by the Python build, which means the on-disk format is compatible and the backlog was not re-alerted. **This is the highest-risk line in the whole deploy.** If it says `firstRun=true`, the Python build did not read the existing baseline; the next slot change will alert normally but everything currently open went unannounced.
- No state-write failure lines — i.e. the uid-1000 fix works against the real volume.
- `free -m` and the container's memory, against Node's 104 MB and the spec's ~35 MB expectation. Record the actual number; the spec asks for confirmation, and if it is far off, say so rather than treating the expectation as met.

- [ ] **Step 5: Rehearse the rollback**

The spec: "Rollback is reverting the tag in `apps.auto.tfvars` and running `deploy.sh`. Rehearse it once on melanzana before jeffco's unit is touched."

```bash
# Revert to the TypeScript image.
sed -i '' 's/image_tag = "v2.0.0"/image_tag = "v1.2.0"/' infra/apps.auto.tfvars
grep -A2 'melanzana = {' infra/apps.auto.tfvars
./infra/deploy.sh
./infra/logs.sh melanzana --freshness=5m
```

Expected: melanzana restarts on the TypeScript image, `jeffco: unchanged` again, and the logs show the old startup line (`health=off statusChannel=main`) rather than the Python one. That is a proven rollback path.

```bash
# Roll forward again.
sed -i '' 's/image_tag = "v1.2.0"/image_tag = "v2.0.0"/' infra/apps.auto.tfvars
./infra/deploy.sh
./infra/logs.sh melanzana --freshness=5m
```

Expected: back on Python, `firstRun=false` again — which now also proves the TypeScript and Python builds read each other's `state.json` in both directions.

- [ ] **Step 6: Watch it for real**

The 48-hour shadow run is waived, so this watch is the only live evidence before the port owns the channel alone.

```bash
sleep 900
./infra/logs.sh melanzana --freshness=20m --limit=100
```

Check: ticks are landing at the expected cadence, no repeated `tick failed`, no `state write` lines, and if any slot appeared, exactly one alert was posted for it. Then check the Discord channel itself — a payload the harness proved identical still has to actually arrive.

- [ ] **Step 7: Commit and record the result**

```bash
git add infra/apps.auto.tfvars
git commit -m "Deploy melanzana v2.0.0 (Python)"
git push origin main
```

Record in the commit body or a follow-up note: the observed resident memory against the ~35 MB expectation, and that the 48-hour shadow run was waived.

---

## Self-review

**Spec coverage.** Every section of the design maps to a task:

| Spec section | Task |
| --- | --- |
| Repo layout, toolchain, uv workspace | 1 |
| `timing.py` + the lockout hazard comment | 2 |
| `state.py` | 3 |
| `health.py`, the heartbeat's configured local time, pinned America/Denver | 4 |
| `config.py` env primitives, per-app schema | 5, 12 |
| `discord.py` transport, ops messages, status-webhook fallback | 6 |
| The Monitor contract, `Message.covers`, async `render`, `heartbeat_extras` | 1 (contract), 12 (implementation) |
| Library boundaries; filtering and auth deliberately out | 7 (contract docstring), 9–12 (app side) |
| Post-failure semantics (divergence #1) | 7, 8 |
| `SourceBusy` cadence hold | 8 |
| Parity layer 1 — fixture byte-for-byte | 9, enforced in 13 |
| Parity layer 2 — translated tests, tests first | 2–12 |
| Parity layer 3 — differential harness | 13 |
| Parity layer 4 — 48-hour shadow run | **waived**, recorded above |
| Deployment, safety gates, rollback | 14, 15, 16 |
| Acceptance criteria | 13 (dump), 16 (rollback, deploy) |

**Deferred items stay deferred**, as the spec asks: jeffco's port, the fashionjobs app, `run_once()`/timers, hoisting jeffco's lockout guard, the external dead-man's-switch, and melanzana's own ops channel (`statusChannel=main` is expected in Task 16's log check, not fixed).

**Test count.** ~50 translations of the surviving TypeScript tests, plus the library's new seams:

| File | Tests |
| --- | --- |
| `tests/lib/test_types.py` | 7 |
| `tests/lib/test_timing.py` | 7 |
| `tests/lib/test_state.py` | 9 |
| `tests/lib/test_health.py` | 12 |
| `tests/lib/test_config.py` | 19 |
| `tests/lib/test_discord.py` | 12 |
| `tests/lib/test_runner_tick.py` | 14 |
| `tests/lib/test_runner_liveness.py` | 9 |
| `tests/lib/test_runner_forever.py` | 8 |
| `tests/melanzana/test_cowlendar.py` | 7 |
| `tests/melanzana/test_detector.py` | 5 |
| `tests/melanzana/test_alert.py` | 7 |
| `tests/melanzana/test_config.py` | 4 |
| `tests/melanzana/test_monitor.py` | 5 |
| **Total** | **125** |

The 11 health-server assertions and the 3 `HEALTH_PORT` config tests are gone with the server, as the spec directs. Revision 2 also cut nine tests that restated the implementation or re-tested a library primitive through a second layer — a URL assertion that was a strict substring subset of the byte-identical one above it, `issubclass(SourceBusy, Exception)`, dataclass attribute access, and three melanzana config tests already covered in `tests/lib/test_config.py` — and added twenty-two covering the corrections above. The net growth is entirely in failure paths.

**Type consistency.** `Payload`/`Embed`/`Field`/`Message`/`HeartbeatExtras`/`OpsLabels`/`Monitor` and the three colours are all defined in Task 1 and used unchanged after. `HealthState.items_tracked` is the single name for melanzana's `slotsTracked` and jeffco's `jobsTracked`, and `OpsLabels.tracked_noun` is why that rename is safe. `run_tick` returns `set[str]` in Task 7 and is consumed as `current_keys` in Task 8. `load_state` returns `LoadedState(keys, corrupt)` in Task 3 and both fields are read in Task 8. `heartbeat_extras` is a `Callable[[], HeartbeatExtras]` in `run_liveness`'s signature and is satisfied by the bound method `monitor.heartbeat_extras` in Task 8 and by the class `HeartbeatExtras` itself (a zero-argument callable returning defaults) in the tests. `format_alert(slots, *, booking_url, mention_everyone)` has the same signature in Tasks 11, 12, and 13. `DiscordPostError.status_code` is set in Task 6 and read via `.retryable` in Task 7.

**Import direction.** `types` is a leaf and imports nothing from the library. `timing` and `state` are leaves too. Then `config → {timing, types}`, `health → ∅`, `discord → {types, health}`, `runner → everything`. No cycles, and nothing points backwards: `discord` no longer imports `RunnerConfig` (the status-URL fallback is a config property), and `melanzana.alert` no longer imports from `discord` (the colours live in `types`).

**Failure-mode coverage.** Every path that can produce silence rather than an alert has a test: first-run suppression, a corrupt baseline, a withheld key after a retryable post failure, a *banked* key plus an ops alert after a permanent one, a `render` that raises, a `render` that omits an item, a state write that fails, a `heartbeat_extras` that raises, and an ops post that fails while the death latch is being set.

