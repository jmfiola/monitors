# Jeffco Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port `jeffco-sub-monitor` from TypeScript to `apps/jeffco` on the existing `lib/monitor`, changing the library exactly once.

**Architecture:** The library already owns the loop. jeffco supplies four contract methods plus its own source client (auth, token cache, lockout ceiling), its High-School filter, its DST-sensitive date rendering, and one-message-per-job alerts. The one library change is a busy-stall threshold so a shared-login collision stops claiming the monitor is down.

**Tech Stack:** Python 3.13, `uv` workspace, `httpx`, `pytest`, `ruff`, `mypy --strict`, Docker, Terraform.

**Spec:** [`docs/superpowers/specs/2026-08-21-jeffco-port-design.md`](../specs/2026-08-21-jeffco-port-design.md) — read it before Task 1.

**Reference implementation:** `apps/melanzana/` is the same shape, already proven byte-identical against its TypeScript original. Where this plan says "follow melanzana's pattern", read that code rather than inventing a new one — the pattern has a passing differential harness behind it.

## Global Constraints

- **Python 3.13**, `uv` for everything. `mypy --strict` and `ruff check`/`ruff format --check` clean at the end of every task.
- **Tests before implementation**, every task, and the RED failure must be captured and reported.
- **`jeffco-sub-monitor` is live and load-bearing for a real person's work.** Task 1 edits its test fixtures and expectations; nothing else in that repo changes, and its own suite must stay green.
- **No secret in a log line, an error message, or a committed file.** SFE error pages embed a live `;jsessionid=`, and the PIN closely resembles the user id — so error messages carry a status and a byte count, never a body slice, never a URL, never the user id.
- **`America/Denver` is pinned in code**, never read from the environment. `JeffcoConfig.timezone` stays a field so tests can pass `UTC` and prove the date code honours the zone it is given.
- **The four fixtures are byte-identical between the two repos**, enforced by `cmp` in `tools/parity-diff.sh`.
- **`mypy --strict` implies `--no-implicit-reexport`** — monkeypatch through the string form (`monkeypatch.setattr("jeffco.sfe.time.time", …)`), never `import x as x` in library or app code.
- **ruff groups `monitor`, `melanzana`, and `jeffco` as third-party.** On `I001`, run `uv run ruff check --fix .` and take its ordering. `ruff format` may rewrap multi-argument calls; accept it.
- **`ruff`'s `B008`** forbids a call in a default expression; hoist a module-level constant.
- **`BLE` is not selected but `RUF100` is**, so a `# noqa: BLE001` will be flagged as an unused suppression. Keep the `except Exception` and its comment; drop the noqa.
- **`timeout(1)` does not exist on this macOS box.** Use `(cmd & echo $! > /tmp/x.pid); sleep N; kill "$(cat /tmp/x.pid)"`.
- **Docker builds may hang** if `docker-credential-desktop` is wedged. Diagnose with `echo 'https://index.docker.io/v1/' | docker-credential-desktop get` (a healthy helper answers instantly). Recover with `pkill -f docker-credential-desktop`, then a config with no `credsStore`.

## File Structure

```
lib/monitor/src/monitor/
├── health.py       MODIFIED  busy-stall threshold in should_alert_stall
├── config.py       MODIFIED  busy_stall_alert_sec on RunnerConfig
├── discord.py      MODIFIED  format_status_alert gains a "busy" kind
└── runner.py       MODIFIED  run_liveness gains a "busy" outcome

apps/jeffco/
├── pyproject.toml            name = "jeffco", depends on monitor + httpx
└── src/jeffco/
    ├── __init__.py
    ├── types.py              Job, JobDay
    ├── schools.py            DEFAULT_HS_SCHOOLS, folds, pattern, partition_jobs
    ├── dates.py              day/time labels, parse_job_days, format_job_days, format_approximate
    ├── sfe.py                SfeHttpError, token/date helpers, filter, parse_jobs, SfeClient
    ├── alert.py              format_job_alerts, heartbeat_extras content
    ├── config.py             JeffcoConfig, LABELS, LOG_PREFIX, load_config
    ├── monitor.py            JeffcoMonitor — the four contract methods
    └── main.py               wiring

tests/jeffco/
├── __init__.py
├── fixtures/                 4 anonymized fixtures, cmp-identical to the TS repo
└── test_{schools,dates,sfe_pure,sfe_client,alert,config,monitor}.py

tools/dump_payloads_jeffco.py            NEW  the Python side of jeffco's harness
tools/parity-diff.sh                     MODIFIED  add jeffco's cases
~/personal/jeffco-sub-monitor/
├── test/fixtures/*.json                 MODIFIED  anonymized
├── test/*.test.ts                       MODIFIED  expectations that quote names
└── tools/dump-payloads.ts               NEW  the TypeScript side of jeffco's harness
```

**No Dockerfile work.** The root `Dockerfile` already takes `--build-arg APP=`, which is why it was written that way. Adding jeffco needs one `uv.lock` regeneration and one workspace member, nothing more.

## Two implementation simplifications, both deliberate

These change *how* the code works, not what it emits. Both are called out so a reviewer does not read them as unfaithful ports.

1. **`zonedWallClock`'s two-pass offset hack collapses to one line.** The TypeScript renders a timestamp, reads the offset in effect at that instant, re-parses, and re-reads — because `Intl` can only *format* an offset, so naming a wall-clock time in a zone requires guessing and correcting. Python constructs the wall-clock time directly: `datetime(y, m, d, 0, 0, 0, tzinfo=ZoneInfo(tz)).isoformat()` resolves the offset **for that local time**, which is exactly what the two passes were approximating. The DST-day correctness the comment describes is preserved and is now structural rather than iterative.

2. **`httpx` owns the cookie jar.** The TypeScript hand-rolls a `Map` because `fetch` has no jar and would drop cookies set on the 302 hop. `httpx.AsyncClient` has a correct, domain-aware jar. Keep the **manual single redirect** — it exists for the off-origin refusal and the 307/308 method-preservation guard, which are security properties, not cookie plumbing.

---

### Task 1: Anonymize the fixtures, in both repos

First because everything downstream reads these files, and because they currently hold real district employees' names and employee IDs. Both repos are edited in the same task so the two copies never diverge — `cmp` is the gate that keeps the differential harness meaningful.

**Files:**
- Modify: `~/personal/jeffco-sub-monitor/test/fixtures/{available-jobs,job-detail-single,job-detail-contiguous,job-detail-multiday}.json`
- Modify: any `~/personal/jeffco-sub-monitor/test/*.test.ts` asserting a name or employee id
- Create: `tests/jeffco/fixtures/` (**five** copies — the four JSON fixtures plus `login-page.html`), `tests/jeffco/__init__.py`

**Interfaces:** none — data only.

**Before you touch that repo:** it has a **pre-existing uncommitted change** to
`.env.example` (a comment trimmed and `POLL_INTERVAL_SEC` moved from 20 to 60) that is
**not yours**. Leave it alone and never `git add -A` there — stage only the fixture and
test files you edit, by name. Its suite is green at 173 passing before you start; that
is the number to match afterwards.

- [ ] **Step 1: Find every real name and id**

```bash
cd ~/personal/jeffco-sub-monitor
python3 - <<'PY'
import json, glob, collections
names, ids = collections.Counter(), collections.Counter()
for path in glob.glob('test/fixtures/*.json'):
    raw = json.load(open(path))
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ('employeeFirstName', 'employeeLastName', 'teacher', 'weekDay') and isinstance(v, str):
                    names[(k, v)] += 1
                if k in ('employeeId',) and v is not None:
                    ids[(k, v)] += 1
                walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(raw)
print('names:'); [print('  ', k, v) for k, v in sorted(names.items())]
print('ids:');   [print('  ', k, v) for k, v in sorted(ids.items())]
PY
grep -rnF -f /tmp/real-surnames.txt test/*.test.ts src/ || echo "(no source/test references)"
```

Put the real surnames in a scratch file outside the repo and grep with `-f`, rather
than typing them into a command that lands in this document or in a shell history
that gets committed. The values this step discovers are real district employees'
names and employee ids; they are deliberately **not** recorded anywhere in this repo.

Record the complete list. `weekDay` is a day name, not a person — leave it alone; it is in the query only to confirm the walk reaches nested objects.

- [ ] **Step 2: Choose stable replacements and write them down**

Use obviously-fictional names that keep the same *shape* (a real first and last name, mixed case), and employee ids of the same digit count so nothing that formats them changes width. Map each distinct real value to exactly one fake, and reuse the mapping across all four fixtures — a name appearing in two fixtures must become the same fake in both, or a cross-fixture test breaks.

Suggested, adjust if the real data has more distinct values:

| Real | Fake |
| --- | --- |
| first/last name of teacher A | `Alex` / `Rivera` |
| first/last name of teacher B | `Sam` / `Okonkwo` |
| employee id A (5 digits) | `10001` |
| employee id B (5 digits) | `10002` |

**The real column is deliberately blank, and must stay blank.** Recording the mapping
here would make the anonymization trivially reversible and would put two real district
employees' names and employee ids into a tracked file — which is the exact thing this
task exists to remove. Keep the real values in a scratch file outside the repo for the
length of this task, then delete it.

What a later maintainer actually needs is the *fake* column, so they can tell that a
name in a fixture is fictional. That is above, and it is enough.

- [ ] **Step 3: Apply it to the TypeScript repo's fixtures**

```bash
cd ~/personal/jeffco-sub-monitor
python3 - <<'PY'
import json, glob, pathlib
# Fill both maps from the scratch file written in Step 2. They are left unpopulated
# here on purpose: the keys are real employees' names and ids, and this file is tracked.
MAP_STR = {}  # {"<real first A>": "Alex", "<real last A>": "Rivera", ...}
MAP_INT = {}  # {<real id A>: 10001, <real id B>: 10002}
assert MAP_STR and MAP_INT, "populate from the scratch file before running"
FIELDS_STR = {"employeeFirstName", "employeeLastName", "teacher"}
FIELDS_INT = {"employeeId"}

def walk(o):
    if isinstance(o, dict):
        return {k: (MAP_STR.get(v, v) if k in FIELDS_STR and isinstance(v, str)
                    else MAP_INT.get(v, v) if k in FIELDS_INT
                    else walk(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [walk(v) for v in o]
    return o

for path in sorted(glob.glob('test/fixtures/*.json')):
    p = pathlib.Path(path)
    original = p.read_text()
    data = walk(json.loads(original))
    # Preserve the file's existing indentation so the diff is only the values.
    indent = 2 if original.lstrip().startswith('{\n  ') or original.lstrip().startswith('[\n  ') else 2
    p.write_text(json.dumps(data, indent=indent) + ('\n' if original.endswith('\n') else ''))
    print('rewrote', path)
PY
git diff --stat
```

Then read the diff. **Only values should have changed.** If the reformat moved unrelated lines, revert and do a targeted `sed` on the specific values instead — a fixture whose formatting churned is harder to review and the point is that these files are trustworthy.

- [ ] **Step 4: Update the TypeScript expectations and prove its suite is still green**

```bash
cd ~/personal/jeffco-sub-monitor
grep -rnF -f /tmp/real-values.txt test/ src/
# Replace each with its mapped fake, then:
npm test
```
Expected: **173** passing, same as before. **If the count changed, stop** — a test was deleted rather than updated.

- [ ] **Step 5: Copy into this repo and prove byte-equality**

```bash
cd ~/personal/monitors
mkdir -p tests/jeffco/fixtures && touch tests/jeffco/__init__.py
for f in available-jobs job-detail-single job-detail-contiguous job-detail-multiday; do
  cp "$HOME/personal/jeffco-sub-monitor/test/fixtures/$f.json" "tests/jeffco/fixtures/$f.json"
  cmp "$HOME/personal/jeffco-sub-monitor/test/fixtures/$f.json" "tests/jeffco/fixtures/$f.json" \
    && echo "$f: identical ($(wc -c < tests/jeffco/fixtures/$f.json | tr -d ' ') bytes)"
done
grep -rniF -f /tmp/real-values.txt tests/jeffco/fixtures/ \
  && echo "REAL DATA STILL PRESENT" || echo "no real names or ids remain"
rm -f /tmp/real-values.txt /tmp/real-surnames.txt   # the scratch files, gone
```

Then sweep the **whole tracked tree of both repos**, not just the fixtures — the values
leak into test expectations and into prose, and a sweep scoped to `test/` and `src/`
missed three tracked docs when this was first done:

```bash
for repo in ~/personal/monitors ~/personal/jeffco-sub-monitor; do
  git -C "$repo" grep -inF -f /tmp/real-values.txt -- . || echo "$repo: clean"
done
```

**`login-page.html` IS copied** — an earlier draft of this plan said not to, on the reasoning that nothing in the Python port parses login HTML beyond a token regex. That reasoning was backwards: `extract_token` and `token_expiry_unix` are exactly that regex and that JWT, and testing them against the real page is strictly stronger than against a synthetic string. Checked before accepting it: the fixture carries no `;jsessionid=`, its bearer token expired 2026-08-14, and the JWT's `sub` claim already holds obvious placeholders (`userId: 11111`, `username: "999999"`, `clientId: "0000"`), so whoever built it already sanitised it. Add it to the `cmp` gate in Task 9 alongside the other four — both implementations read it, so byte-equality matters.

- [ ] **Step 6: Commit, both repos**

```bash
cd ~/personal/jeffco-sub-monitor
git add test && git commit -m "Anonymize the test fixtures

They carried real teachers' names and employee ids; the Python port copies
them byte-for-byte, so they are fictionalized in both repos at once."

cd ~/personal/monitors
git add tests/jeffco && git commit -m "Copy jeffco's anonymized fixtures"
```

---

### Task 2: The library's busy-stall threshold

The one library change. Read the spec's "Why the library changes once" first — the obvious version of this is wrong and reintroduces a bug this project already shipped and fixed.

**Files:**
- Modify: `lib/monitor/src/monitor/health.py`, `config.py`, `discord.py`, `runner.py`
- Modify: `tests/lib/test_health.py`, `test_config.py`, `test_discord.py`, `test_runner_liveness.py`, and **`test_runner_forever.py`** — its `test_a_status_post_failure_never_disturbs_polling` forces an ops post with `SourceBusy` plus `STALL_ALERT_SEC=1`, which stops working once busy takes 3600s. Since `busy_stall_alert_sec` is deliberately not env-readable, squeeze it with `replace(cfg_for(...), busy_stall_alert_sec=1)` and keep both the `SourceBusy` and the `[10, 10]` cadence assertion.

**Interfaces:**
- Consumes: existing library types.
- Produces:
  - `HealthState` gains `busy_only: bool` — true when every failure since the last success was a busy signal.
  - `should_alert_stall(state, now, stall_sec, busy_stall_sec) -> bool` replaces the bare `is_stalled` call at the alert site; `is_stalled` itself is unchanged and still exported.
  - `RunnerConfig.busy_stall_alert_sec: int = 3600`.
  - `format_status_alert` accepts `kind: Literal["death", "busy", "recovery"]`.
  - `run_liveness`'s `outcome` accepts `"busy"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/lib/test_health.py`:

```python
def test_a_busy_only_absence_uses_the_longer_threshold() -> None:
    # A shared-login collision is not a fault, so ten minutes of it must not page
    # anyone — but it is still an absence of data, so it cannot be ignored either.
    h = replace(init_health(1000), busy_only=True)
    assert should_alert_stall(h, 1000 + 600, 600, 3600) is False
    assert should_alert_stall(h, 1000 + 3599, 600, 3600) is False
    assert should_alert_stall(h, 1000 + 3600, 600, 3600) is True


def test_a_real_fault_uses_the_short_threshold_even_after_busy_ticks() -> None:
    # The moment anything other than a busy signal happens, the generous window is
    # gone: this is how a permanently-400ing upstream still surfaces, and how a
    # genuine outage is not hidden behind an hour of grace.
    h = replace(init_health(1000), busy_only=False)
    assert should_alert_stall(h, 1000 + 600, 600, 3600) is True


def test_busy_does_not_reset_the_success_clock() -> None:
    # The clock keeps running through a busy period. Resetting it is what would
    # produce indefinite silence under a heartbeat still saying "still watching".
    h = replace(init_health(1000), busy_only=True)
    assert is_stalled(h, 1000 + 3600, 600) is True
```

Append to `tests/lib/test_config.py`:

```python
def test_busy_stall_defaults_to_an_hour_and_is_not_read_from_the_environment() -> None:
    # Deliberately not wired to an env var yet: a variable nothing consumes is
    # config that silently does nothing. Same reasoning that withheld HEARTBEAT_AT.
    cfg = load_runner_config(
        {"DISCORD_WEBHOOK_URL": WEBHOOK, "BUSY_STALL_ALERT_SEC": "7"},
        labels=LABELS, log_prefix="x", default_poll_interval_sec=10,
    )
    assert cfg.busy_stall_alert_sec == 3600
```

Append to `tests/lib/test_discord.py`:

```python
def test_a_busy_alert_names_the_cause_instead_of_claiming_the_monitor_is_down() -> None:
    state = replace(init_health(1000), last_success_unix=1000, consecutive_failures=40)
    payload = format_status_alert("busy", LABELS, state, 5000)
    embed = payload.embeds[0]
    assert payload.content is None            # ops messages never ping
    assert "in use elsewhere" in embed.description
    assert "blocked or down" not in embed.description
    assert embed.color == RED
```

Append to `tests/lib/test_runner_liveness.py`:

```python
async def test_a_busy_outcome_latches_one_busy_alert_and_recovers() -> None:
    posts = Collector()
    cfg = cfg_with()
    health = init_health(1000)
    for now in (1600, 4000):             # 600 and 3000 elapsed: under the hour.
                                         # NOT 4600 — that is exactly 3600 elapsed, and
                                         # is_stalled is >=, so it would alert and
                                         # contradict the assertion below.
        health = await run_liveness(
            cfg, health, outcome="busy", items_tracked=0, now=now,
            heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
        )
    assert posts.posts == []              # a short collision says nothing at all
    health = await run_liveness(
        cfg, health, outcome="busy", items_tracked=0, now=1000 + 3600,
        heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
    )
    assert len(posts.posts) == 1
    assert "in use elsewhere" in posts.posts[0].embeds[0].description
    assert health.death_alerted is True    # latched, so it does not repeat

    health = await run_liveness(
        cfg, health, outcome="success", items_tracked=3, now=1000 + 3700,
        heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
    )
    assert len(posts.posts) == 2           # one recovery
    assert health.busy_only is False


async def test_one_real_fault_forfeits_the_busy_grace_window() -> None:
    posts = Collector()
    cfg = cfg_with()
    health = await run_liveness(
        cfg, init_health(1000), outcome="busy", items_tracked=0, now=1100,
        heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
    )
    assert health.busy_only is True
    health = await run_liveness(
        cfg, health, outcome="failure", items_tracked=0, now=1200,
        heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
    )
    assert health.busy_only is False
    health = await run_liveness(
        cfg, health, outcome="busy", items_tracked=0, now=1700,
        heartbeat_extras=HeartbeatExtras, poster=posts, log=noop_log,
    )
    # 700s elapsed, past the 600s fault threshold, and busy_only is already
    # forfeited — so this alerts as a death, not as a busy signal.
    assert len(posts.posts) == 1
    # "blocked or down" lives in the per-app death FOOTER (labels.death_footer), not the
    # description — this module's LABELS uses "footer." — and moving it into the
    # description would change melanzana's death payload and break parity. Assert the
    # death shape instead.
    assert "3 consecutive failures" in posts.posts[0].embeds[0].description
    assert "in use elsewhere" not in posts.posts[0].embeds[0].description
    assert posts.posts[0].embeds[0].footer_text == LABELS.death_footer
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest tests/lib -q`
Expected: FAIL — `ImportError: cannot import name 'should_alert_stall'` plus `TypeError` on the unexpected `busy_only` field.

- [ ] **Step 3: Implement**

In `health.py`, add the field to `HealthState` and `init_health` (`busy_only: bool = False` / `busy_only=False`), and add:

```python
def should_alert_stall(
    state: HealthState, now: int, stall_sec: int, busy_stall_sec: int
) -> bool:
    """True when the absence of data has gone on long enough to say something.

    Two thresholds, because two different things look identical to `is_stalled`.
    A shared-login collision is not a fault — the upstream is reachable and the
    cause is the account holder using their own account — so ten minutes of it must
    not page anyone. But it is still an absence of data, so it cannot be exempt:
    an upstream that answers "busy" *permanently* would otherwise produce
    indefinite silence underneath a heartbeat still reporting "still watching",
    which is the failure this project trades everything else against.

    So a busy-only absence gets an hour, and the moment any other fault occurs
    `busy_only` is forfeited and the short threshold governs again.
    """
    return is_stalled(state, now, busy_stall_sec if state.busy_only else stall_sec)
```

In `config.py`, add `busy_stall_alert_sec: int = 3600` to `RunnerConfig`, below `post_spacing_sec`, with a comment saying it is deliberately not read from the environment yet and why.

In `discord.py`, widen the `kind` literal and add the branch:

```python
    if kind == "busy":
        return Payload(
            embeds=(
                Embed(
                    title=f"⚠️ {labels.name} — no successful poll",
                    description=(
                        f"No successful poll since <t:{state.last_success_unix}:R>. "
                        f"Every attempt since then reported the source busy, which "
                        f"usually means the account is in use elsewhere — so this is "
                        f"probably someone working, not an outage. Still retrying; "
                        f"you'll get one more message when it recovers."
                    ),
                    color=RED,
                    footer_text="Liveness alert — no action needed unless it persists.",
                ),
            )
        )
```

In `runner.py`, widen `run_liveness`'s `outcome` to `Literal["success", "busy", "failure"]` and restructure the failure half:

```python
    if outcome == "success":
        updated = replace(
            updated, last_success_unix=now, consecutive_failures=0,
            items_tracked=items_tracked, busy_only=False,
        )
        # ... existing recovery block unchanged ...
    else:
        # A busy tick keeps busy_only true only if it was already true; a fault
        # clears it permanently until the next success. That asymmetry is the whole
        # mechanism: grace is granted while nothing but collisions happen, and
        # revoked by the first real fault.
        busy_only = updated.busy_only and outcome == "busy"
        updated = replace(
            updated,
            consecutive_failures=updated.consecutive_failures + 1,
            busy_only=busy_only,
        )
        if (
            should_alert_stall(updated, now, cfg.stall_alert_sec, cfg.busy_stall_alert_sec)
            and not updated.death_alerted
        ):
            await post_status(
                format_status_alert("busy" if busy_only else "death", cfg.labels, updated, now)
            )
            updated = replace(updated, death_alerted=True)
```

`init_health` must seed `busy_only=False`, and the first busy tick after a success has to *set* it — so the `busy_only` expression above needs `updated.busy_only or updated.consecutive_failures == 0` reasoning. Implement it as: `busy_only = outcome == "busy" and (updated.busy_only or updated.consecutive_failures == 0)`, and let the tests decide whether that is right — `test_a_busy_outcome_latches_one_busy_alert_and_recovers` fails if the first busy tick does not set it.

Then update `run_forever`'s `SourceBusy` branch to pass `outcome="busy"` instead of `"failure"`.

- [ ] **Step 4: Verify**

```
uv run pytest tests/lib -q            # expect 110 (103 lib + 7 new)
uv run pytest -q                       # expect 139 (110 lib + 29 melanzana)
uv run mypy --strict lib apps tests tools
uv run ruff check . && uv run ruff format --check .
./tools/parity-diff.sh                 # MUST still be identical — melanzana's payloads are untouched
```

The harness matters here: `format_status_alert` gained a branch, and melanzana's death and recovery cases must render byte-identically still.

- [ ] **Step 5: Commit**

```bash
git add lib tests/lib
git commit -m "Add a busy-stall threshold to the monitor library

A shared-login collision is not an outage, but it is still an absence of
data, so it gets an hour rather than an exemption."
```

---

### Task 3: The workspace member, types, and the High-School filter

`schools.ts` is pure string work with no I/O and no dates — the cheapest module to port and a good place to establish the app's shape. It also has the **largest test file** of the port: 41 cases, counted from vitest, not grepped.

**Files:**
- Create: `apps/jeffco/pyproject.toml`, `apps/jeffco/src/jeffco/{__init__,types,schools}.py`
- Modify: root `pyproject.toml` (add the member), `uv.lock` (regenerated)
- Test: `tests/jeffco/test_schools.py`

**Interfaces:**
- Produces: `Job` and `JobDay` frozen dataclasses; `DEFAULT_HS_SCHOOLS: tuple[str, ...]`; `normalize_school_name(raw: str) -> str`; `parse_school_list(raw: str) -> set[str]`; `is_high_school(raw_name: str, allow: AbstractSet[str]) -> bool`; `partition_jobs(jobs: Sequence[Job], allow: AbstractSet[str]) -> tuple[list[Job], list[str]]`.

`partition_jobs` returns a tuple rather than the TypeScript's `{hs, unmatched}` object because Python has no anonymous record and a two-field dataclass for one call site is ceremony. The unmatched list stays **sorted and de-duplicated**, and carries **raw** spellings so they can be pasted into `HS_SCHOOLS` verbatim.

- [ ] **Step 1: Add the workspace member**

`apps/jeffco/pyproject.toml` — copy `apps/melanzana/pyproject.toml` and change `melanzana` to `jeffco` in `name` and `packages`. Add `"apps/jeffco"` to the root `pyproject.toml`'s `[tool.uv.workspace] members`, then:

```bash
uv sync && uv run python -c "import jeffco; print('member ok')"
```

- [ ] **Step 2: Write the failing test**

`tests/jeffco/test_schools.py` — translate all **41** cases from `~/personal/jeffco-sub-monitor/test/schools.test.ts`. That file is larger than it looks because it exercises the fold table exhaustively. The cases below carry the reasoning and must survive translation; the rest are systematic coverage of normalization and must be ported too, not summarized:

```python
from jeffco.schools import (
    DEFAULT_HS_SCHOOLS,
    is_high_school,
    normalize_school_name,
    parse_school_list,
    partition_jobs,
)
from jeffco.types import Job


def job(location: str, job_id: int = 1) -> Job:
    return Job(
        job_id=job_id, location_name=location,
        job_start="2026-10-16T13:45Z", job_end="2026-10-16T20:30Z",
    )


def test_a_roster_spelling_matches_sfes_spelling() -> None:
    # The whole point of normalization: the district roster says "Ralston Valley
    # High School" and SFE says "RALSTON VALLEY HS".
    assert normalize_school_name("Ralston Valley High School") == normalize_school_name(
        "RALSTON VALLEY HS"
    )


def test_the_longest_fold_wins_so_no_stray_senior_survives() -> None:
    # FOLDS is ordered longest-first specifically so "SENIOR HIGH SCHOOL" collapses
    # in one step instead of leaving a bare SENIOR that the pattern would then match.
    assert normalize_school_name("Foo Senior High School") == "FOO HS"


def test_junior_high_deliberately_does_not_match() -> None:
    # Folding JUNIOR HIGH, or treating bare JR as a token, would make every middle
    # school a high school.
    assert is_high_school("SOMEWHERE JUNIOR HIGH", set()) is False


def test_a_combined_campus_does_match() -> None:
    assert is_high_school("POMONA JUNIOR/SENIOR", set()) is True
    assert is_high_school("ALAMEDA INTERNATIONAL JR/SR", set()) is True


def test_the_apostrophe_survives_because_both_sides_run_the_same_folds() -> None:
    assert is_high_school("D'Evelyn Junior/Senior", set()) is True


def test_an_unrecognizable_name_needs_the_allow_list() -> None:
    # DORAL ACADEMY and WARREN TECH say nothing about grade level. This is exactly
    # what the gap report exists to surface.
    assert is_high_school("DORAL ACADEMY OF COLORADO", set()) is False
    assert is_high_school("DORAL ACADEMY OF COLORADO", parse_school_list("Doral Academy of Colorado")) is True


def test_partition_returns_raw_unmatched_spellings_sorted_and_deduplicated() -> None:
    # Raw, so they can be pasted into HS_SCHOOLS verbatim; deduplicated because the
    # list accumulates across a process lifetime and the heartbeat renders it.
    hs, unmatched = partition_jobs(
        [job("LITTLE ELE"), job("GOLDEN HIGH SCHOOL", 2), job("LITTLE ELE", 3), job("Bar Middle", 4)],
        parse_school_list("\n".join(DEFAULT_HS_SCHOOLS)),
    )
    assert [j.job_id for j in hs] == [2]
    assert unmatched == ["Bar Middle", "LITTLE ELE"]


def test_the_shipped_list_is_all_recognized_by_its_own_normalizer() -> None:
    # A typo in DEFAULT_HS_SCHOOLS is inert rather than harmful, but a shipped entry
    # that its own filter would not match is a sign the folds changed underneath it.
    allow = parse_school_list("\n".join(DEFAULT_HS_SCHOOLS))
    for name in DEFAULT_HS_SCHOOLS:
        assert is_high_school(name, allow) is True
```

Translate the remaining cases from the TypeScript file — separator parsing (`;` and newline), empty entries dropped, punctuation folding (`.`, `,`, `/`, `-` to space), and case insensitivity.

- [ ] **Step 3: Run it and see it fail**

Run: `uv run pytest tests/jeffco/test_schools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jeffco.schools'`

- [ ] **Step 4: Implement**

`types.py` mirrors `~/personal/jeffco-sub-monitor/src/types.ts`, snake_cased, with the same four required fields and six optional ones:

```python
@dataclass(frozen=True)
class Job:
    """One available job, narrowed to the fields the monitor reads.

    Only the first four are validated at parse time. The rest are optional by
    construction, not just by convention, because SFE may omit any of them — and
    every consumer already guards.
    """
    job_id: int
    location_name: str
    job_start: str          # ISO-8601 UTC, no seconds: "2026-10-16T13:45Z"
    job_end: str
    classf_name: str | None = None
    employee_first_name: str | None = None
    employee_last_name: str | None = None
    days_of_week: str | None = None
    duration_type: str | None = None
    job_status: str | None = None


@dataclass(frozen=True)
class JobDay:
    """One worked day, resolved from GET /api/job/{id}. Epoch seconds."""
    start_unix: int
    end_unix: int
```

`schools.py`: **copy the 22 entries of `DEFAULT_HS_SCHOOLS` verbatim from `~/personal/jeffco-sub-monitor/src/schools.ts:16-39`** — do not retype them from memory, the exact spellings are the data. Bring its whole docstring across too: over-inclusion is free because the endpoint only returns jobs this substitute is assigned for, so a wrong entry is inert while an omission costs a real job.

Then the folds, applied longest-first, and the pattern:

```python
_FOLDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bSENIOR HIGH SCHOOL\b"), "HS"),
    (re.compile(r"\bSR HIGH SCHOOL\b"), "HS"),
    (re.compile(r"\bSENIOR HIGH\b"), "HS"),
    (re.compile(r"\bSR HIGH\b"), "HS"),
    (re.compile(r"\bHIGH SCHOOL\b"), "HS"),
)

#: JUNIOR HIGH deliberately does not fold and bare JR is deliberately not a token:
#: either would make every middle school a match. JR SR is the abbreviated combined
#: campus, which is a high school.
_HS_PATTERN = re.compile(r"(^|\s)(HS|SENIOR|JR SR)(\s|$)")
```

`normalize_school_name` uppercases, strips, replaces `[.,/\-]` with a space, collapses whitespace, applies the folds in order, and collapses again. `is_high_school` is the **union** of allow-list membership and the pattern — union rather than intersection because a miss costs a real job while a false positive costs one ignored Discord message.

- [ ] **Step 5: Verify and commit**

```
uv run pytest tests/jeffco -q                  # expect 41
uv run pytest -q                                # expect 181
uv run mypy --strict lib apps tests tools
uv run ruff check . && uv run ruff format --check .
```

```bash
git add apps/jeffco pyproject.toml uv.lock tests/jeffco
git commit -m "Port jeffco's High-School filter

Union of allow-list and pattern: a miss costs a real job, a false positive
costs one ignored message."
```

---

### Task 4: Dates, and the DST case

The module the spec names as the most likely source of a real divergence.

**Files:**
- Create: `apps/jeffco/src/jeffco/dates.py`
- Test: `tests/jeffco/test_dates.py`

**Interfaces:**
- Produces: `format_day_label(unix_sec: int, timezone: str) -> str` → `"Fri Oct 16"`; `format_time_label(unix_sec: int, timezone: str) -> str` → `"7:45 AM"`; `parse_job_days(detail: object) -> list[JobDay]`; `format_job_days(days: Sequence[JobDay], timezone: str) -> str`; `format_approximate(job: Job, timezone: str) -> str`.

- [ ] **Step 1: Write the failing test**

**Enumerate, do not illustrate.** An earlier draft of this task showed 11 example tests under the heading "translate all 14", and the illustration was read as the deliverable — six real cases went unported, including the configured-zone one this project's design doc explicitly requires. So: open `~/personal/jeffco-sub-monitor/test/dates.test.ts`, list its 14 `it(...)` cases by name, port each one's assertions, and then add the DST case below. The examples that follow are a subset for reference, not the set to write:

Every timestamp in these tests must be a pinned constant, computed once and written
into the file — never `datetime.now()`, or the DST tests pass or fail depending on the
month you run them. Compute them first:

```bash
python3 -c "
from datetime import datetime
from zoneinfo import ZoneInfo
D = ZoneInfo('America/Denver')
for label, dt in [
    ('MDT autumn 07:45', datetime(2026,10,16,7,45,tzinfo=D)),
    ('MDT autumn 15:30', datetime(2026,10,16,15,30,tzinfo=D)),
    ('MST winter 07:45', datetime(2026,12,4,7,45,tzinfo=D)),
    ('spring-forward day 00:00', datetime(2026,3,8,0,0,tzinfo=D)),
    ('day after spring-forward 07:45', datetime(2026,3,9,7,45,tzinfo=D)),
]:
    print(f'{label:32} {int(dt.timestamp())}  {dt.isoformat()}')
"
```

Then the tests, with those constants at the top of the file:

```python
from jeffco.dates import (
    format_approximate,
    format_day_label,
    format_job_days,
    format_time_label,
    parse_job_days,
)
from jeffco.types import Job, JobDay

DENVER = "America/Denver"
# Pinned from the command above. MDT is UTC-6, MST is UTC-7; the March pair
# straddles the 2026 spring-forward transition.
MDT_OCT16_0745 = ...   # fill from the command's output
MDT_OCT16_1530 = ...
MDT_OCT19_0745 = ...
MDT_OCT19_1530 = ...
MDT_OCT19_1200 = ...
MIDNIGHT_MAR8 = ...
MST_MAR6_0745 = ...
MST_MAR6_1530 = ...
MDT_MAR9_0745 = ...
MDT_MAR9_1530 = ...


def test_day_and_time_labels() -> None:
    assert format_day_label(MDT_OCT16_0745, DENVER) == "Fri Oct 16"
    assert format_time_label(MDT_OCT16_0745, DENVER) == "7:45 AM"
    assert format_time_label(MDT_OCT16_1530, DENVER) == "3:30 PM"


def test_the_hour_has_no_leading_zero_and_the_minute_does() -> None:
    # This is why strftime is not used: %-I is not portable and %I would give "07".
    assert format_time_label(MDT_OCT16_0745, DENVER).startswith("7:45")


def test_noon_and_midnight_render_as_12() -> None:
    # A 12-hour clock built by hand gets 0 and 12 wrong if you use `% 12` alone.
    assert format_time_label(MIDNIGHT_MAR8, DENVER) == "12:00 AM"


def test_a_uniform_schedule_states_the_times_once() -> None:
    days = [JobDay(MDT_OCT16_0745, MDT_OCT16_1530), JobDay(MDT_OCT19_0745, MDT_OCT19_1530)]
    assert format_job_days(days, DENVER) == "Fri Oct 16 · Mon Oct 19, 7:45 AM – 3:30 PM"


def test_differing_schedules_carry_their_own_times() -> None:
    # Stating one schedule that is wrong for some days is worse than being verbose.
    days = [JobDay(MDT_OCT16_0745, MDT_OCT16_1530), JobDay(MDT_OCT19_0745, MDT_OCT19_1200)]
    out = format_job_days(days, DENVER)
    assert out == "Fri Oct 16 7:45 AM – 3:30 PM · Mon Oct 19 7:45 AM – 12:00 PM"


def test_a_job_spanning_the_spring_forward_keeps_each_days_own_wall_clock() -> None:
    # THE case this module is most likely to get wrong. Both days start at 7:45
    # local, but the UTC offset differs across the transition — so a naive
    # implementation that computes one offset and reuses it renders one day an hour
    # off. Both must read 7:45 AM.
    days = [JobDay(MST_MAR6_0745, MST_MAR6_1530), JobDay(MDT_MAR9_0745, MDT_MAR9_1530)]
    out = format_job_days(days, DENVER)
    assert out == "Fri Mar 6 · Mon Mar 9, 7:45 AM – 3:30 PM"
    assert out.count("7:45 AM") == 1  # uniform, so stated once


def test_no_days_says_so_rather_than_rendering_nothing() -> None:
    assert format_job_days([], DENVER) == "Dates unavailable — check SFE"


def test_parse_job_days_sorts_and_tolerates_garbage() -> None:
    detail = {"jobDetails": [
        {"subStart": "2026-10-19T13:45:00Z", "subEnd": "2026-10-19T21:30:00Z"},
        {"subStart": "2026-10-16T13:45:00Z", "subEnd": "2026-10-16T21:30:00Z"},
        {"subStart": None, "subEnd": "2026-10-20T21:30:00Z"},
        "not an object",
    ]}
    days = parse_job_days(detail)
    assert len(days) == 2
    assert days[0].start_unix < days[1].start_unix


def test_parse_job_days_returns_empty_on_a_shape_change() -> None:
    # Tolerant by design: the caller falls back to the approximate rendering.
    assert parse_job_days({}) == []
    assert parse_job_days({"jobDetails": "nope"}) == []
    assert parse_job_days(None) == []


def test_approximate_is_labelled_approximate() -> None:
    j = Job(job_id=1, location_name="X", job_start="2026-10-16T13:45Z",
            job_end="2026-10-19T21:30Z", days_of_week="F M")
    out = format_approximate(j, DENVER)
    assert "approximate, check SFE" in out
    assert "(days: F M)" in out


def test_approximate_degrades_rather_than_raising_on_an_unparseable_date() -> None:
    # This is the path that has to hold when everything else has already failed.
    j = Job(job_id=1, location_name="X", job_start="not-a-date", job_end="also-not")
    assert format_approximate(j, DENVER) == "Dates unavailable — check SFE"
```

- [ ] **Step 2: Run it and see it fail**

Run: `uv run pytest tests/jeffco/test_dates.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jeffco.dates'`

- [ ] **Step 3: Implement**

```python
"""Date rendering for the alert. All labels are built from explicit tables rather
than strftime: %a and %b are locale-dependent, and %-I (hour without a leading
zero) is a glibc/BSD extension Python does not guarantee. This is the same
construction melanzana's alert.py uses, which has a byte-identical differential
result behind it.
"""

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")   # isoweekday() - 1
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_day_label(unix_sec: int, timezone: str) -> str:
    """"Fri Oct 16", in the given zone."""
    local = datetime.fromtimestamp(unix_sec, ZoneInfo(timezone))
    return f"{WEEKDAYS[local.isoweekday() - 1]} {MONTHS[local.month - 1]} {local.day}"


def format_time_label(unix_sec: int, timezone: str) -> str:
    """"7:45 AM", in the given zone. Hour unpadded, minute padded."""
    local = datetime.fromtimestamp(unix_sec, ZoneInfo(timezone))
    hour = local.hour % 12 or 12          # 0 -> 12 and 12 -> 12; `% 12` alone gives 0
    meridiem = "AM" if local.hour < 12 else "PM"
    return f"{hour}:{local.minute:02d} {meridiem}"
```

Note the `WEEKDAYS` tuple here starts at Monday and indexes with `isoweekday() - 1`, which differs from melanzana's Sunday-first `isoweekday() % 7` — melanzana was matching JavaScript's `getUTCDay()`, and this module has no such constraint. Do not copy melanzana's indexing blindly; the tests pin the output either way.

`parse_job_days` reads `detail["jobDetails"]`, keeps entries where both `subStart` and `subEnd` are strings that parse, and sorts by `start_unix`. Use `datetime.fromisoformat` — Python 3.11+ accepts the trailing `Z` — inside a `try/except ValueError` that skips the row. `format_job_days` renders each day's `"start – end"` (note: an **en dash**, U+2013, matching the TypeScript), collapses to one shared time range when every day's range is identical, and joins day labels with `" · "` (U+00B7). `format_approximate` degrades to `"Dates unavailable{tokens} — check SFE"` when either endpoint fails to parse.

- [ ] **Step 4: Verify and commit**

```
uv run pytest tests/jeffco -q      # expect 55
uv run pytest -q                    # expect 195
uv run mypy --strict lib apps tests tools && uv run ruff check .
```

```bash
git add apps/jeffco/src/jeffco/dates.py tests/jeffco/test_dates.py
git commit -m "Port jeffco's date rendering

Explicit tables rather than strftime, and a test for a job spanning the
spring-forward transition."
```

---

### Task 5: sfe.py — the pure functions

Split from the client deliberately: these are testable without any HTTP, and they carry the API's hard-won quirks. About 20 of `sfe.test.ts`'s 40 tests land here.

**Files:**
- Create: `apps/jeffco/src/jeffco/sfe.py` (pure functions only; the client is Task 6)
- Test: `tests/jeffco/test_sfe_pure.py`

**Interfaces:**
- Produces: `SFE_BASE`, `AVAILABLE_JOBS_URL`, `TOKEN_REFRESH_MARGIN_SEC = 120`, `MAX_LOGIN_FAILURES = 3`, `LOGIN_BACKOFF_SEC = 3600`, `BROWSER_HEADERS`; `SfeHttpError(Exception)` with `.status: int`; `is_account_busy(err: object) -> bool`; `extract_token(html: str) -> str`; `token_expiry_unix(jwt: str) -> int`; `zoned_wall_clock(unix_sec: int, timezone: str, time: str) -> str`; `build_available_filter(now_unix: int, window_days: int, timezone: str) -> dict[str, object]`; `parse_jobs(body: object) -> list[Job]`.

`zoned_date_string` and `zoned_offset` from the TypeScript **do not survive** — they existed only to feed the two-pass offset hack. See the plan header's simplification note.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from jeffco.sfe import (
    SfeHttpError, build_available_filter, extract_token, is_account_busy,
    parse_jobs, token_expiry_unix, zoned_wall_clock,
)


def test_only_a_400_is_a_busy_signal() -> None:
    # Narrow on purpose. A 5xx, a network error, or an unparseable body is a real
    # fault and must still earn exponential backoff.
    assert is_account_busy(SfeHttpError(400, "x")) is True
    assert is_account_busy(SfeHttpError(500, "x")) is False
    assert is_account_busy(SfeHttpError(401, "x")) is False
    assert is_account_busy(RuntimeError("400")) is False


def test_the_token_comes_out_of_the_script_assignment() -> None:
    html = "<html><script>var token = 'Bearer abc.def-123_x';</script></html>"
    assert extract_token(html) == "abc.def-123_x"


def test_a_missing_token_is_how_login_failure_is_detected() -> None:
    # Deliberately not a credential-specific signature: SFE plausibly locks an
    # account after repeated failures, so that response was never probed. Absence
    # of a token catches wrong credentials, an Imperva challenge, and a changed
    # login flow with the same error.
    with pytest.raises(SfeLoginError) as exc:
        extract_token("<html>login form</html>")
    # The body may hold a session id, so only its size may appear.
    assert "login form" not in str(exc.value)
    assert "22 bytes" in str(exc.value) or "bytes" in str(exc.value)


def test_token_expiry_reads_the_exp_claim() -> None:
    import base64, json
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1799999999}).encode()).rstrip(b"=")
    jwt = b"h." + payload + b".s"
    assert token_expiry_unix(jwt.decode()) == 1799999999


def test_token_expiry_rejects_malformed_tokens_without_quoting_them() -> None:
    for bad in ("notajwt", "a.b", "a.!!!.c"):
        with pytest.raises(SfeTokenError) as exc:
            token_expiry_unix(bad)
        assert bad not in str(exc.value)


def test_token_expiry_rejects_a_payload_with_no_usable_exp() -> None:
    import base64, json
    for claims in ({}, {"exp": "soon"}, {"exp": None}):
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
        with pytest.raises(SfeTokenError):
            token_expiry_unix(b"h." + payload + b".s")


def test_the_filter_stamps_each_end_with_its_own_offset() -> None:
    # A window spanning a DST change must be correct at BOTH ends. The TypeScript
    # needed two passes per end to achieve this; zoneinfo resolves it directly.
    f = build_available_filter(MST_MAR6_1200, 7, "America/Denver")
    opt = f["filterOption"]
    assert opt["jobStart"] == "2026-03-06T00:00:00-07:00"   # MST
    assert opt["jobEnd"] == "2026-03-13T23:59:59-06:00"     # MDT, after the change


def test_the_filter_uses_zero_as_the_no_filter_sentinel() -> None:
    # [0] means "no filter" for the three id lists. An empty array is NOT accepted.
    opt = build_available_filter(MST_MAR6_1200, 180, "America/Denver")["filterOption"]
    assert opt["locationIdList"] == [0]
    assert opt["locationGroupIdList"] == [0]
    assert opt["classificationIdList"] == [0]
    assert opt["jobInstruction"] == ["NONE"]
    assert opt["teacher"] is None
    assert opt["jobId"] == ""


def test_job_start_is_local_midnight_so_it_is_never_in_the_past() -> None:
    # The API rejects a past jobStart but accepts today's local midnight, which is
    # why the window is rebuilt every tick — that also handles midnight rollover.
    opt = build_available_filter(MST_MAR6_1200, 1, "America/Denver")["filterOption"]
    assert opt["jobStart"].endswith("T00:00:00-07:00")


def test_parse_jobs_keeps_valid_rows_and_drops_malformed_ones() -> None:
    body = [
        {"jobId": 1, "locationName": "A", "jobStart": "s", "jobEnd": "e"},
        {"jobId": "2", "locationName": "B", "jobStart": "s", "jobEnd": "e"},   # id not a number
        {"jobId": 3, "locationName": "C", "jobStart": "s"},                    # missing jobEnd
        "not an object",
    ]
    jobs = parse_jobs(body)
    assert [j.job_id for j in jobs] == [1]


def test_parse_jobs_raises_when_every_row_fails() -> None:
    # One bad row is upstream noise and quietly reduces coverage. EVERY row failing
    # is a shape change, and swallowing it would leave the monitor permanently blind
    # while still reporting success.
    with pytest.raises(SfeShapeError, match="3 row"):
        parse_jobs([{"jobId": "x"}, {"nope": 1}, "str"])


def test_parse_jobs_rejects_a_non_array_body() -> None:
    with pytest.raises(SfeShapeError):
        parse_jobs({"jobs": []})


def test_parse_jobs_accepts_an_empty_list() -> None:
    # No jobs available is a normal, frequent answer — not an error.
    assert parse_jobs([]) == []
```

Translate the remaining pure-function cases from `sfe.test.ts`. Pin `MST_MAR6_1200` with the same command Task 4 used.

- [ ] **Step 2: Run it and see it fail**

Run: `uv run pytest tests/jeffco/test_sfe_pure.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jeffco.sfe'`

- [ ] **Step 3: Implement**

Three distinct exception types rather than the TypeScript's bare `Error`, because the tests above discriminate on them and `pytest.raises(Exception)` asserts nothing:

```python
class SfeHttpError(Exception):
    """A non-ok HTTP status, carrying the status as a field.

    The caller's retry policy turns on it, and recovering a number by re-parsing
    prose breaks the next time the message is reworded.
    """
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class SfeLoginError(Exception):
    """The login handshake produced no token."""


class SfeTokenError(Exception):
    """The token is not a usable JWT."""


class SfeShapeError(Exception):
    """The response's shape is not what the API contract says."""
```

`zoned_wall_clock` is the simplification:

```python
def zoned_wall_clock(unix_sec: int, timezone: str, time: str) -> str:
    """"2026-03-08T00:00:00-07:00" — the given wall-clock time on the local date at
    `unix_sec`, stamped with the offset in effect **at that wall-clock time**.

    The TypeScript needs two passes here: `Intl` can only format an offset, so it
    renders once with the offset at `unix_sec`, parses to find roughly which instant
    is meant, then re-renders with the offset actually in effect there. On a
    spring-forward day the offset at noon (-06:00) is not the offset at midnight
    (-07:00), and stamping the former onto the latter names an instant on the
    previous local day — which the API rejects as a past `jobStart`, blinding the
    monitor for that whole day.

    `zoneinfo` resolves the offset for a local time directly, so the correction is
    structural rather than iterative.
    """
    zone = ZoneInfo(timezone)
    local_date = datetime.fromtimestamp(unix_sec, zone).date()
    hour, minute, second = (int(part) for part in time.split(":"))
    stamped = datetime(
        local_date.year, local_date.month, local_date.day, hour, minute, second, tzinfo=zone
    )
    return stamped.isoformat()
```

`extract_token` uses `re.compile(r"var\s+token\s*=\s*'Bearer\s+([A-Za-z0-9._-]+)'")` and raises `SfeLoginError` naming only `len(html)`. `token_expiry_unix` splits on `.`, requires exactly three segments, and base64url-decodes the middle with padding restored: `base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))` — Python is strict about padding where JavaScript's `base64url` is not, and this is the single most likely place a naive translation breaks. It must reject a non-`int` or non-finite `exp`, and never include the token in any message.

- [ ] **Step 4: Verify and commit**

```
uv run pytest tests/jeffco -q      # expect ~75
uv run mypy --strict lib apps tests tools && uv run ruff check .
```

```bash
git add apps/jeffco/src/jeffco/sfe.py tests/jeffco/test_sfe_pure.py
git commit -m "Port jeffco's SFE helpers

zoned_wall_clock replaces the two-pass Intl offset hack; zoneinfo resolves a
local time's offset directly."
```

---

### Task 6: sfe.py — the authenticated client

The security-dense half. Every guard here exists because of something observed against the real service, and the comments are the record of that — carry them across.

**Files:**
- Modify: `apps/jeffco/src/jeffco/sfe.py` (append the client)
- Test: `tests/jeffco/test_sfe_client.py`

**Interfaces:**
- Produces: `SfeClient(client: httpx.AsyncClient, user_id: str, pin: str, timezone: str, window_days: int, now_unix: Callable[[], int], log: Callable[[str], None])` with `async fetch_available_jobs() -> list[Job]` and `async fetch_job_detail(job_id: int) -> object`.

A class, not a closure-returning factory: the TypeScript used a closure to hold the jar, token, expiry, and failure counters, and a Python class holds the same state more legibly. `httpx.AsyncClient` supplies the jar (see the header's simplification note), so the client's own state is the token, its expiry, and the two lockout counters.

- [ ] **Step 1: Write the failing test**

**These tests are translations, not inventions.** The assertions already exist in
`~/personal/jeffco-sub-monitor/test/sfe.test.ts` (40 cases across the pure functions
and the client); roughly 20 belong here. Read that file and port each case, keeping
its assertions. Use `httpx.MockTransport` for the transport, as `tests/lib/test_discord.py`
does, and a mutable `clock = [1000]` list with `now_unix=lambda: clock[0]` so time can
be advanced without sleeping.

This table is the checklist of what must survive translation — every row is a real
behaviour with a reason, and a row you cannot find in the TypeScript is a row to raise
with me rather than skip:

| Test | Must pin |
| --- | --- |
| happy path | one login, then the token reused across calls. A 60s cadence that re-logs in every tick is a login hammer. |
| 401 once | one re-auth and one retry, transparently. |
| 401 twice | gives up and raises. A credential problem must fail the tick and enter backoff, not loop. |
| 307/308 | raises without following. Following would re-POST the PIN to wherever `Location` points. Message names the status only. |
| off-origin `Location` | raises. A browser would not carry SFE's cookies cross-origin, and this also blocks a foreign page planting its own `var token = 'Bearer …'`. The message must **not** contain the foreign host. |
| missing / unparseable `Location` | raises, status only. |
| second redirect | raises. One hop is the verified flow; a chain means a changed login or an Imperva challenge. |
| HTML body with a 200 | raises **before** the status check. An Imperva challenge or Tomcat page must never be parsed as data. The message carries the status and byte count and **never** a body slice — SFE error pages embed a live `;jsessionid=`. |
| JSON error body | the API's own `message` is surfaced (`"Start date must be in the future."`) but nothing else of the body is. |
| non-JSON success body | raises, naming only the length. |
| 3 login failures | the fourth call makes **no network request at all** and raises "suppressed"; after `LOGIN_BACKOFF_SEC` it resumes. **The most important test in this file** — a wrong PIN on the poll cadence is ~1,440 attempts a day against an account a real person works from. |
| success after failures | the failure counter resets to zero. |
| token inside the refresh margin | re-logs in 120s before `exp` rather than eating a 401. |
| fresh token already inside the margin | logs the clock-skew warning. Without it, a skewed container clock presents as "every call re-authenticates" with no explanation. |
| credential sweep | assert the PIN **and** the user id appear in no captured log line and no raised message. The PIN closely resembles the id, so leaking the id leaks most of the PIN. |
| `fetch_job_detail` on a filled job | returns 200 and renders completely — verified against the real service, so a job claimed between the list fetch and this call still alerts. |

- [ ] **Step 2: Run it and see it fail, then implement**

Structure the client as:

- `_absorb`/`_cookie_header` **deleted** — `httpx.AsyncClient` handles cookies. Construct the client with `follow_redirects=False` so the manual hop is the only redirect handling.
- **Every send must go through ONE guarded helper**, and this is a leak channel the TypeScript does not have. `fetch` with `redirect: 'manual'` never parses `Location`; httpx parses it even with `follow_redirects=False`, to build `next_request`, and its `RemoteProtocolError` **quotes the header** — which for SFE legitimately carries a live `;jsessionid=`. Re-raise status-only with `from None`, and make the helper the only place `self._client` is touched so a later call site cannot be added unguarded. Assert the absence of a sentinel from `str`, `repr`, **and the formatted traceback**; the message alone is not enough, because the chained original is what surfaces in a log.
- Compare origins on **`(scheme, netloc)`**, not `netloc` alone — the TypeScript compared `URL.origin`, and an `http://` downgrade would put session cookies on the wire.
- `_post_following_one_redirect(url, data)` — exactly one hop. Raise on 307/308 (`"would re-send the credentials; refusing"`), raise on a missing or unparseable `Location`, raise if `urlparse(target).netloc != urlparse(SFE_BASE).netloc`, and raise on a second redirect. **Status-only messages throughout.**
- `_handshake()` — GET `/logOnInitAction.do`, POST `/logOnAction.do` with `{"userID": …, "userPin": …, "bootstrapDevice": ""}` form-encoded, then `extract_token(response.text)` and `token_expiry_unix`. **Assign both fields or neither**: a token paired with a stale expiry either re-logs in on every call or trusts an expiry that has already passed.
- `_login()` — the lockout wrapper. Check `now < blocked_until` first and raise `SfeLoginError` naming the remaining seconds; on success reset the counter; on failure increment, and at `MAX_LOGIN_FAILURES` set `blocked_until = now + LOGIN_BACKOFF_SEC` and log why.
- `_ensure_token()` — re-login when `token is None or now >= expires_at - TOKEN_REFRESH_MARGIN_SEC`.
- `_api_request(path, method, json_body, label, allow_retry=True)` — ensure token, send with `authorization: Bearer …`, on 401 with `allow_retry` clear the token and recurse once with `allow_retry=False`, then `_assert_not_html` **before** the status check, then raise `SfeHttpError(status, f"SFE {label} failed: HTTP {status}{message}")`, then parse JSON.
- `fetch_available_jobs()` builds the filter from `now_unix()` every call and returns `parse_jobs(body)`.
- `fetch_job_detail(job_id)` GETs `/api/job/{job_id}`. It returns 200 even for an already-filled job, so a job claimed between the list fetch and this call still renders completely.

- [ ] **Step 3: Verify, with a credential sweep**

```
uv run pytest tests/jeffco -q      # expect ~95
uv run mypy --strict lib apps tests tools && uv run ruff check .
grep -rnE 'user_id|pin' apps/jeffco/src/jeffco/sfe.py | grep -iE 'log\(|f"|raise' || echo "no credential reaches a message"
```

- [ ] **Step 4: Commit**

```bash
git add apps/jeffco/src/jeffco/sfe.py tests/jeffco/test_sfe_client.py
git commit -m "Port jeffco's authenticated SFE client

Three login failures then one attempt an hour: a wrong PIN on the poll
cadence is ~1,440 attempts a day against an account someone works from."
```

---

### Task 7: alert.py — one message per job

**Files:**
- Create: `apps/jeffco/src/jeffco/alert.py`
- Test: `tests/jeffco/test_alert.py`

**Interfaces:**
- Consumes: `monitor.types` (`Embed`, `Field`, `GREEN`, `HeartbeatExtras`, `Message`, `Payload`), `jeffco.types.Job`.
- Produces: `MAX_GAP_NAMES = 10`; `format_job_alerts(alerts: Sequence[tuple[Job, str]]) -> list[Message]`; `heartbeat_extras_for(unmatched: Sequence[str]) -> HeartbeatExtras`.

`format_job_alerts` takes `(job, date_line)` pairs rather than the TypeScript's `AlertJob` record — a two-field dataclass for one call site is ceremony, and the tuple keeps the date-resolution boundary visible.

- [ ] **Step 1: Write the failing test**

The 21 cases from `discord.test.ts` that belong to jeffco (the ops-message ones are now library-owned and already tested there). The ones carrying reasoning:

```python
def test_one_message_per_job_never_batched_into_one_payload() -> None:
    # Discord MERGES embeds sharing an identical url, keeping only the first one's
    # title and description — and SFE has no per-job deep link, so every embed
    # carries the same url. Batching meant a multi-job tick displayed exactly one
    # job and silently discarded the rest, with state recording all of them as
    # announced. Verified against a real webhook.
    messages = format_job_alerts([(job(1, "GOLDEN HIGH SCHOOL"), "Fri Oct 16"),
                                  (job(2, "ARVADA WEST HS"), "Mon Oct 19")])
    assert len(messages) == 2
    assert [m.covers for m in messages] == [("1",), ("2",)]
    assert all(len(m.payload.embeds) == 1 for m in messages)


def test_the_alert_body_is_the_layout_the_account_holder_was_shown() -> None:
    # Labeled lines in one description rather than embed fields: fields reflow into
    # columns on desktop and reorder relative to the description.
    m = format_job_alerts([(job(1, "GOLDEN HIGH SCHOOL", classf_name="SEC MATH",
                                first="Alex", last="Rivera"), "Fri Oct 16, 7:45 AM – 3:30 PM")])[0]
    d = m.payload.embeds[0].description
    assert d == ("**Subject:** SEC MATH\n"
                 "**Dates:** Fri Oct 16, 7:45 AM – 3:30 PM\n"
                 "**Teacher:** Alex Rivera")
    assert m.payload.embeds[0].title == "🏫 GOLDEN HIGH SCHOOL"


def test_a_missing_subject_says_not_specified_rather_than_going_blank() -> None:
    m = format_job_alerts([(job(1, "GOLDEN HIGH SCHOOL"), "Fri Oct 16")])[0]
    assert "**Subject:** Not specified" in m.payload.embeds[0].description


def test_a_teacher_line_is_omitted_entirely_when_there_is_no_name() -> None:
    # Not an empty "**Teacher:** " line — an absent field should look absent.
    m = format_job_alerts([(job(1, "GOLDEN HIGH SCHOOL", classf_name="SEC MATH"), "Fri Oct 16")])[0]
    assert "Teacher" not in m.payload.embeds[0].description


def test_a_full_day_duration_is_not_mentioned() -> None:
    # FULL is the overwhelming majority; naming it on every alert would be noise.
    m = format_job_alerts(
        [(job(1, "GOLDEN HIGH SCHOOL", duration_type="FULL"), "Fri Oct 16")]
    )[0]
    assert "Duration" not in m.payload.embeds[0].description


def test_a_partial_day_duration_is_mentioned() -> None:
    m = format_job_alerts(
        [(job(1, "GOLDEN HIGH SCHOOL", duration_type="HALF AM"), "Fri Oct 16")]
    )[0]
    assert "**Duration:** HALF AM" in m.payload.embeds[0].description


def test_the_gap_report_lists_the_newest_names_not_the_first() -> None:
    # The list accumulates for the process lifetime, so slicing from the front would
    # give the first ten names ever seen permanent ownership of every slot and a
    # newly discovered campus would never appear.
    extras = heartbeat_extras_for([f"SCHOOL {i}" for i in range(1, 15)])
    assert extras.fields is not None
    value = extras.fields[0].value
    assert "SCHOOL 14" in value
    assert "SCHOOL 1\n" not in value
    assert "…and 4 more" in value


def test_the_gap_report_is_capped_so_it_cannot_make_the_heartbeat_unpostable() -> None:
    # A Discord embed field value is capped at 1024 characters. An uncapped
    # accumulating list would eventually make Discord reject the entire heartbeat —
    # turning the message that proves the monitor is alive into one that never
    # arrives.
    extras = heartbeat_extras_for([f"A VERY LONG SCHOOL NAME NUMBER {i}" for i in range(200)])
    assert extras.fields is not None
    assert len(extras.fields[0].value) <= 1024


def test_gaps_swap_the_footer_for_the_instruction() -> None:
    # This is the seam the library added specifically for jeffco: the field carries
    # the data and the footer carries what to do about it.
    assert heartbeat_extras_for(["DORAL ACADEMY"]).footer_text == (
        "If any of these are high schools, add them to HS_SCHOOLS."
    )


def test_no_gaps_adds_nothing_and_keeps_the_default_footer() -> None:
    extras = heartbeat_extras_for([])
    assert extras.fields == ()
    assert extras.footer_text is None
```

- [ ] **Step 2: Run, fail, implement**

`format_job_alerts` returns one `Message` per job, `covers=(str(job.job_id),)`, each with a single embed: title `f"🏫 {job.location_name}"`, `url=AVAILABLE_JOBS_URL`, the labeled-lines description, `color=GREEN`, footer `"Tap the title to open Available Jobs — go claim it."`. **No cap on the number of messages** — dropping a job is the one failure mode that costs something real, and the only batch big enough to matter is a deliberate `echo '[]' > state.json`.

`heartbeat_extras_for` returns `HeartbeatExtras()` when there are no gaps; otherwise one `Field(name="Schools not on the list", value=…, inline=False)` listing the **last** `MAX_GAP_NAMES` with `f"• {name}"` lines plus `f"\n• …and {rest} more"`, and `footer_text` set to the HS_SCHOOLS instruction.

- [ ] **Step 3: Verify and commit**

```
uv run pytest tests/jeffco -q      # expect ~116
uv run mypy --strict lib apps tests tools && uv run ruff check .
```

```bash
git add apps/jeffco/src/jeffco/alert.py tests/jeffco/test_alert.py
git commit -m "Port jeffco's per-job alerts

One message per job: Discord merges embeds sharing a url, and SFE has no
per-job deep link."
```

---

### Task 8: config, the Monitor implementation, and main

After this task jeffco runs.

**Files:**
- Create: `apps/jeffco/src/jeffco/{config,monitor,main}.py`, `apps/jeffco/.env.example`
- Test: `tests/jeffco/test_config.py`, `tests/jeffco/test_monitor.py`

**Interfaces:**
- Produces: `LOG_PREFIX = "jeffco-sub-monitor"`; `LABELS = OpsLabels(name="Jeffco sub monitor", tracked_noun="high school job(s)", death_footer="Liveness alert — check Available Jobs manually until it clears.")`; `JeffcoConfig` with `runner`, `sfe_user_id`, `sfe_pin`, `hs_schools`, `window_days`, `timezone`; `load_config(env) -> JeffcoConfig`; `JeffcoMonitor` implementing `Monitor[Job]`.

The three `OpsLabels` strings must match `~/personal/jeffco-sub-monitor/src/discord.ts` exactly — the differential harness diffs them.

- [ ] **Step 1: Write the failing tests**

`test_config.py` — the app-specific half of `config.test.ts`'s **16**. `SFE_USER_ID` and `SFE_PIN` required; `HS_SCHOOLS` **additive** (built-in list ∪ configured, never replacing — the realistic edit is "add the school that got missed", and replace semantics would turn that one-liner into a silent loss of 21 campuses); `POLL_INTERVAL_SEC` defaulting to **60**, not 10, and a test asserting that with the reason in a comment; `WINDOW_DAYS` 180; `timezone` fixed to `America/Denver`, not read from the environment.

```python
def test_the_pin_and_id_are_required() -> None:
    with pytest.raises(ConfigError, match="SFE_USER_ID is required"):
        load_config({"DISCORD_WEBHOOK_URL": WEBHOOK})


def test_hs_schools_is_additive_never_replacing() -> None:
    cfg = load_config({**BASE, "HS_SCHOOLS": "Doral Academy of Colorado"})
    assert len(cfg.hs_schools) == len(DEFAULT_HS_SCHOOLS) + 1


def test_the_poll_interval_defaults_to_sixty_not_ten() -> None:
    # Deliberately slower than the official web client's own 30s refresh: the
    # monitor shares one login with the substitute it watches for, and a poll in
    # flight while he is on the site draws an HTTP 400 on one side or a stale
    # listing on the other. Do not lower this while the login is shared.
    assert load_config(BASE).runner.poll_interval_sec == 60


def test_the_zone_is_not_read_from_the_environment() -> None:
    assert load_config({**BASE, "TIMEZONE": "Europe/Berlin"}).timezone == "America/Denver"
```

`test_monitor.py` — the four contract methods. Translate the app-level half of
`~/personal/jeffco-sub-monitor/test/index.test.ts` (the loop half is library-owned and
already covered in `tests/lib/`). Use `httpx.MockTransport` and pinned epochs. Required
cases and what each pins:

| Test | Must pin |
| --- | --- |
| `fetch` filters to HS | only high-school jobs are returned, and only their keys can enter state — a non-HS job entering state would let a school-list edit resurrect stale ids as "new". |
| `fetch` records gaps | an unmatched name reaches `heartbeat_extras()`. |
| gaps accumulate | names persist across ticks rather than being replaced. A school can appear in one poll and be claimed before the next, while the heartbeat fires daily — keeping only the latest tick would drop exactly what the report exists to surface. |
| SFE 400 → `SourceBusy` | so the runner holds cadence instead of escalating. |
| SFE 500 stays `SfeHttpError` | a real fault must still earn backoff. |
| `key` is the job id alone | not a content hash: an edited job must not re-alert, and a job claimed then released **must**, because that is a genuine new opportunity. |
| `render` resolves dates per job | one message per job, each with its own date line. |
| a failed detail fetch degrades one job | the other jobs still render normally, and the degraded one says "approximate, check SFE". Under the library a `render()` raise withholds **every** fresh key, so this matters more than it did in TypeScript. |
| `heartbeat_extras` with no gaps | `fields == ()` and `footer_text is None`, so the library keeps its default footer. |

- [ ] **Step 2: Run, fail, implement**

`JeffcoMonitor.__init__` takes `cfg`, an `SfeClient`, `now_unix`, and `log`, and holds `self._unmatched_seen: set[str]` — accumulated for the process lifetime, with each name logged the first time it is seen because the logs are where this gets noticed within the day.

- `fetch()` → `self._sfe.fetch_available_jobs()`, wrapped so `is_account_busy(err)` re-raises as `SourceBusy`; then `partition_jobs`, absorb the unmatched into `_unmatched_seen`, return the HS jobs. **Only HS keys enter state** — non-HS jobs never do, which keeps the file small and stops a change to the school list resurrecting stale ids as "new".
- `key(job)` → `str(job.job_id)`.
- `render(new)` → for each job, resolve the date line inside a `try`: `parse_job_days(await self._sfe.fetch_job_detail(job.job_id))`, use `format_job_days` when non-empty, else `format_approximate`; on any exception log and fall back to `format_approximate`. Then `format_job_alerts(pairs)`.
- `heartbeat_extras()` → `heartbeat_extras_for(sorted(self._unmatched_seen))`.

`main.py` follows melanzana's exactly: `make_log(LOG_PREFIX)`, `install_shutdown_handlers`, one `httpx.AsyncClient` with a timeout, `system_now` passed to **both** the monitor and `run_forever`, and a `poster` closure. Its app-config log line must not include the user id — the PIN closely resembles it, so printing the id leaks most of the PIN.

Add the `TYPE_CHECKING` protocol assertion at the bottom of `monitor.py`, as melanzana has:

```python
if TYPE_CHECKING:
    def _assert_satisfies_protocol(m: JeffcoMonitor) -> Monitor[Job]:
        return m
```

- [ ] **Step 3: Verify, including a refusal check and a live-config check**

```
uv run pytest -q                                   # expect ~280
uv run mypy --strict lib apps tests tools && uv run ruff check . && uv run ruff format --check .
uv run python -c "
from jeffco.config import load_config
try: load_config({})
except Exception as err: print('refused:', err)
"
```
Expected: `refused: Config error: DISCORD_WEBHOOK_URL is required`.

**Do not run the app against live SFE in this task** — that is Task 11, deliberately scheduled.

- [ ] **Step 4: Commit**

```bash
git add apps/jeffco tests/jeffco
git commit -m "Wire jeffco's config, Monitor implementation, and entrypoint"
```

---

### Task 9: The differential harness for jeffco

The layer the port is trusted on, and the one that replaces the shadow run this cycle deliberately does not do. It must be capable of failing — Task 9 proves that with a perturbation, exactly as melanzana's review did.

**Files:**
- Create: `tools/dump_payloads_jeffco.py`
- Create: `~/personal/jeffco-sub-monitor/tools/dump-payloads.ts`
- Modify: `tools/parity-diff.sh`

**Interfaces:** the two scripts must print byte-identical canonical JSON. Node's `JSON.stringify(canon(x), null, 2)` and Python's `json.dumps(x, sort_keys=True, indent=2, ensure_ascii=False)` are already established as byte-identical — see `tools/dump_payloads.py` and its TypeScript counterpart, and copy their `canon` helper rather than writing a new one.

- [ ] **Step 1: Write both dump scripts**

Frozen clock, identical literals on both sides, cases in this order:

| Case | Input | What it pins |
| --- | --- | --- |
| `1-single-day` | `job-detail-single.json` | the common alert, one day |
| `2-contiguous` | `job-detail-contiguous.json` | multi-day, uniform schedule stated once |
| `3-multiday` | `job-detail-multiday.json` | differing schedules, per-day times |
| `4-batch` | all rows of `available-jobs.json` | **one message per job**, and their order |
| `5-approximate` | a job with **no** resolved days | the degraded date line |
| `6-dst-span` | a synthetic job spanning 2026-03-08 | both days at 7:45 AM local |
| `7-heartbeat-clean` | no gaps | no `fields` key, default footer |
| `8-heartbeat-gaps` | 14 unmatched names | newest 10 + "…and 4 more", swapped footer |
| `9-death` / `10-recovery` | jeffco's own `OpsLabels` | the death footer and tracked noun |

Case 6 is synthetic on both sides and must use the same literal epochs.

**Case 5 calls the approximate formatter directly** — `format_approximate` / `formatApproximate` — rather than simulating a failed fetch. The harness compares *rendering*, and a mocked-failure path would differ between an `httpx.MockTransport` and whatever the TypeScript stubs, making the two sides disagree about something that is not the output. That the failure *routes* to this formatter is a unit test's job (`test_monitor.py`), not the harness's.

**There is deliberately no busy case, and adding one would be a mistake.** Earlier revisions of this plan listed `9-busy`. The TypeScript has no busy Discord payload at all — `discord.ts:168` has exactly one stall message and jeffco's busy handling is a log line at `index.ts:367`. So a busy case could only be built two ways, and both are worthless: the TypeScript script fabricates the Python's new wording, making the case a tautology that prints one literal twice, or the diff fails permanently and destroys the meaning of an empty diff. The busy outcome is [divergence #5](../specs/2026-08-21-jeffco-port-design.md) — deliberate, and therefore exactly what a parity harness must exclude. This is the same constraint the melanzana cycle recorded for invalid dates, and for the same reason.

The busy wording is already unit-tested at `tests/lib/test_discord.py:169`, with further busy coverage in `test_runner_liveness.py`, `test_runner_forever.py`, `test_health.py` and `test_config.py`. Nothing is lost by keeping it out of the harness.

- [ ] **Step 2: Extend `tools/parity-diff.sh`**

Add jeffco's four fixtures to the existing `cmp` gate, then run both jeffco dumps and diff. Keep melanzana's section unchanged and running first — a regression there is the loudest possible signal that the library change broke something.

- [ ] **Step 3: Run it**

```bash
./tools/parity-diff.sh
```
Expected: every fixture `identical`, melanzana identical in both mention states, jeffco identical.

**If the diff is not empty, that is the most valuable output of this entire plan.** Do not adjust either side to force agreement. Paste it and say which side you believe is wrong. Likely culprits in order: the en dash (U+2013) in a time range versus a hyphen; `·` (U+00B7) versus a middle dot lookalike; a `12:00 AM`/`12:00 PM` boundary; and the DST case.

- [ ] **Step 4: Prove the harness can fail**

Perturb one thing in `apps/jeffco/src/jeffco/dates.py` — change the en dash to a hyphen — re-run, confirm a unified diff and a non-zero exit, then restore it and confirm `git diff` is empty and the harness is clean again. Report both outputs. A harness nobody has broken on purpose is not evidence.

- [ ] **Step 5: Commit, both repos**

```bash
git add tools && git commit -m "Add jeffco's differential payload harness"
cd ~/personal/jeffco-sub-monitor && git add tools \
  && git commit -m "Add a payload dump script for the Python port's parity harness"
```

---

### Task 10: Build and verify the image

No Dockerfile changes — the root `Dockerfile` takes `--build-arg APP=`, which is the reason it was written that way. This task proves that claim rather than assuming it.

- [ ] **Step 1: Build**

```bash
cd ~/personal/monitors
docker build --platform linux/amd64 --build-arg APP=jeffco -t jeffco-sub-monitor:v2.0.0 .
docker image inspect jeffco-sub-monitor:v2.0.0 --format '{{.Architecture}}'
```
Expected: build succeeds with **no Dockerfile edit**, architecture `amd64`. If it needs an edit, say what — that falsifies a design claim and is worth reporting loudly.

- [ ] **Step 2: The three probes, as melanzana's Task 14 did**

uid 1000 and `/data` writable; no `.env` in the image; then a container run. For the container run, pass a **deliberately invalid** `SFE_PIN` and confirm it fails the login three times and then pauses — that exercises the lockout ceiling against the real service's rejection path without risking the real account, because three failures is exactly what the guard permits:

```bash
docker run -d --platform linux/amd64 --memory=256m \
  -e DISCORD_WEBHOOK_URL=https://discord.test/webhook \
  -e SFE_USER_ID=00000000 -e SFE_PIN=0000 \
  -e STATE_PATH=/data/state.json \
  -v /tmp/jeffco-data:/data --name jeffco-smoke jeffco-sub-monitor:v2.0.0
sleep 40
docker logs jeffco-smoke
# Measure BEFORE stopping -- `docker stats` reports nothing for a stopped
# container, and this figure is the whole reason the memory cap stays at 256m
# until it is measured rather than predicted.
docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.MemPerc}}' jeffco-smoke
docker stop jeffco-smoke && docker rm jeffco-smoke
```

**Use an obviously-fake user id**, not the real one. Expected: the startup lines, then login failures, then `pausing login attempts for 3600s`. Report the memory figure exactly as printed, without rounding it toward a prediction — the previous cycle predicted ~35 MB and measured 25.3 MiB, and a local Docker Desktop reading is inflated by the VM, so this number is indicative only. The host figure in Task 12 is the one that counts.

- [ ] **Step 3: Push and commit**

```bash
gcloud auth configure-docker us-west1-docker.pkg.dev --quiet
docker tag jeffco-sub-monitor:v2.0.0 us-west1-docker.pkg.dev/cobs-cloud/jeffco/jeffco-sub-monitor:v2.0.0
docker push us-west1-docker.pkg.dev/cobs-cloud/jeffco/jeffco-sub-monitor:v2.0.0
gcloud artifacts docker tags list us-west1-docker.pkg.dev/cobs-cloud/jeffco/jeffco-sub-monitor --project cobs-cloud
```
Both `v1.1.0` and `v2.0.0` must be listed. **If `v1.1.0` is gone there is nothing to roll back to — stop.**

---

### Task 11: Infra wiring

**Files:** `infra/apps.auto.tfvars`, `infra/variables.tf`, `infra/main.tf`, `apps/README.md`

- [ ] **Step 1: Edits**

Bump jeffco's `image_tag` to `"v2.0.0"`; keep `image = "jeffco-sub-monitor"` and `memory = "256m"` — the cap stays until measured, because guessing a footprint is the mistake the previous spec made. Add `jeffco_heartbeat_at` (default `"07:00"`, same validation regex as melanzana's) and wire `HEARTBEAT_AT` into `local.app_env.jeffco` conditionally, matching the existing `STATUS_WEBHOOK_URL` pattern. Update `apps/README.md`'s table.

**Do not touch melanzana's entry.**

- [ ] **Step 2: Verify without applying**

```bash
cd infra && terraform fmt -check && terraform validate && ./deploy.sh --plan
```
Expected: `Plan: 0 to add, 1 to change, 0 to destroy`, an **in-place** update of `google_compute_instance.host`, and the diff showing jeffco `v1.1.0 → v2.0.0` with melanzana's image unchanged. **If anything is destroyed or replaced, stop** — a replaced instance loses the boot disk and both apps' `state.json`.

- [ ] **Step 3: Commit**

```bash
git add infra apps/README.md && git commit -m "Wire HEARTBEAT_AT for jeffco and bump it to v2.0.0"
```

---

### Task 12: The live smoke, the cutover, and the rollback rehearsal

**This task touches a monitor a real person depends on for work.** Read every step before running any of them.

**Timing is part of the task.** Cut over at a quiet hour — after midnight Denver, per the evidence that no busy-signal 400 has ever landed overnight. Do not do this mid-morning.

- [ ] **Step 1: Back up the state before anything**

```bash
cp infra/terraform.tfstate ~/Documents/monitors-tfstate-backup-$(date +%F-%H%M).json
gcloud compute ssh monitors --project cobs-cloud --zone us-west1-b \
  --command 'sudo cat /var/lib/jeffco-data/state.json' > ~/Documents/jeffco-baseline-backup-$(date +%F-%H%M).json
wc -c ~/Documents/jeffco-baseline-backup-*.json
```

The baseline backup is new relative to melanzana's deploy and it matters more: jeffco's `state.json` holds the ids of every job already announced, and losing it means either re-announcing a backlog or (worse) silently suppressing what is currently open.

- [ ] **Step 2: The one live authenticated smoke**

The single call the offline harness cannot make. Run it **locally**, not on the host, with the real credentials from `infra/terraform.tfvars`, against a **scratch** webhook so nothing reaches the real channel:

```bash
cd ~/personal/monitors
SFE_USER_ID=… SFE_PIN=… DISCORD_WEBHOOK_URL=<scratch> STATE_PATH=/tmp/jeffco-smoke.json \
  uv run python -c "
import asyncio, os, httpx
from jeffco.config import load_config
from jeffco.monitor import JeffcoMonitor
from jeffco.sfe import SfeClient
from monitor.config import make_log
from monitor.timing import system_now

async def go():
    cfg = load_config(os.environ)
    log = make_log('smoke')
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False) as c:
        # Keyword arguments deliberately. An earlier draft of this script passed
        # JeffcoMonitor's log and now_unix positionally and had them the wrong way
        # round, which fails at the first log call -- i.e. partway through login,
        # spending one of the three attempts this account gets per hour.
        sfe = SfeClient(
            client=c, user_id=cfg.sfe_user_id, pin=cfg.sfe_pin, timezone=cfg.timezone,
            window_days=cfg.window_days, now_unix=system_now, log=log,
        )
        m = JeffcoMonitor(cfg=cfg, sfe=sfe, log=log, now_unix=system_now)
        jobs = await m.fetch()
        print(f'authenticated and fetched {len(jobs)} high school job(s)')
        for j in jobs[:3]:
            print('  ', j.job_id, j.location_name)
        print('gaps:', m.heartbeat_extras().fields)
asyncio.run(go())
"
```

Expected: an `authenticated; token valid for …s` line and a job count. **One run only.** If it fails on auth, stop and report — do not retry more than twice, because three failures trip the hour-long lockout on a real person's account.

- [ ] **Step 3: Deploy**

```bash
./infra/deploy.sh
```
Expected: `melanzana: unchanged`, `jeffco: changed, will restart`, `startup complete`, `exit status 0`.

- [ ] **Step 4: Verify, and know what you are looking for**

```bash
./infra/deploy.sh --verify
./infra/logs.sh jeffco --freshness=10m
```

Check, in order of how bad it is to get wrong:

1. **`firstRun=False`.** If this says `true`, the Python build did not read the TypeScript build's baseline, and **every job currently open will go unannounced**. That is the failure that looks exactly like everything being fine. If you see it, roll back immediately (Step 6) and investigate before trying again.
2. `heartbeatAt=07:00`, `statusChannel=separate` (jeffco has its own ops webhook).
3. No `state write` lines, no tracebacks, no `webhook refused`.
4. `melanzana-monitor` still `Up` on `v2.0.1` and untouched.
5. The memory figure, reported plainly.

- [ ] **Step 5: Watch for one real cycle**

jeffco polls every 60s and jobs appear through the day. Watch at least 15 minutes and confirm: ticks landing at cadence, `authenticated; token valid for …s` appearing once rather than every tick (a token being re-fetched every tick means the refresh margin or the container clock is wrong), and any `account busy elsewhere` line holding cadence rather than escalating.

- [ ] **Step 6: Rehearse the rollback**

```bash
sed -i '' 's/image_tag = "v2.0.0"/image_tag = "v1.1.0"/' infra/apps.auto.tfvars   # jeffco's line only — check the diff
./infra/deploy.sh && ./infra/logs.sh jeffco --freshness=5m
```
Expected: the TypeScript startup line returns, and `firstRun=false` — which proves the TypeScript build reads the baseline the Python build wrote, i.e. the format is compatible in both directions. Then roll forward and confirm again.

- [ ] **Step 7: Commit and record**

```bash
git add infra/apps.auto.tfvars && git commit -m "Deploy jeffco v2.0.0 (Python)"
```

Record: the observed memory against jeffco's Node figure of ~94 MiB, whether `firstRun=False` held, and whether the cap can safely drop to `128m` next cycle.

---

## Self-review

**Spec coverage.** Every section of the design maps to a task: fixture anonymization → 1; the busy-stall library change → 2; `schools`/`dates`/`sfe`/`alert`/wiring → 3-8; the harness and the no-shadow-run decision → 9; the image → 10; infra → 11; the live smoke, cutover, and rollback → 12. The three deferrals (coalescing, lockout hoisting, melanzana's rebuild) appear in no task, deliberately.

**Test arithmetic**, counted with `vitest --reporter=json` rather than grepped — an earlier draft of this plan undercounted by 28 because `grep -cE '^\s+it\('` misses nested and parameterized cases. **173** exist in TypeScript: `schools` 41, `sfe` 40, `index` 24, `discord` 21, `config` 16, `dates` 14, `timing` 7, `state` 5, `health` 5. Library-owned and dropped: `timing`+`state`+`health`+`index` = **41**. Translated: **132**. Expected end state: 132 existing Python + ~8 library (Task 2) + 41 schools + 14 dates + ~40 sfe + 21 alert + ~16 config + ~9 monitor ≈ **281**. Each task states its own expected count; if one disagrees with reality, the actual number is right and the plan's estimate is wrong — report it rather than inventing a test.

**Type consistency.** `Job`/`JobDay` are defined in Task 3 and used unchanged after. `SfeHttpError.status` is set in Task 5 and read by `is_account_busy` in Task 5 and by the client in Task 6. `format_job_alerts` takes `(Job, str)` pairs in Task 7 and is called that way in Task 8. `heartbeat_extras_for(Sequence[str]) -> HeartbeatExtras` in Task 7 is called by `JeffcoMonitor.heartbeat_extras()` in Task 8. `HealthState.busy_only` is added in Task 2 and read only by `should_alert_stall`.

**The weakest points, named rather than buried.** The DST case in Task 4 and case 6 in Task 9 are where I expect a real divergence. Task 12's live smoke is a single shot against a shared login with a three-strike lockout, so it is scripted rather than improvised. And Task 2's `busy_only` transition logic is the one piece of the library change I could not fully settle on paper — the plan says so explicitly and lets the tests decide.

