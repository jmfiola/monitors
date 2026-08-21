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
├── health.py    HealthState, is_stalled, should_alert_stall, should_heartbeat
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

**`Message.covers`** names the item keys a message announces. jeffco posts one
message per job, so when a single post fails the runner must know which keys to
withhold. melanzana's one message covers every fresh key, so the same shape serves
both.

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

### The heartbeat lands at a wall-clock hour

`HEARTBEAT_INTERVAL_SEC` alone anchors the heartbeat to process start, so it arrives
at whatever time the last deploy happened, re-anchors on restart, and creeps later
daily. `HEARTBEAT_AT` (`HH:MM`, America/Denver) fixes it: due when the local date has
changed since the last heartbeat *and* the local time is at or past it. Both
conditions are needed — the date check is what stops it firing repeatedly all day.

Unset, the interval behaviour applies. Production runs `07:00` for both apps: early
enough to read over coffee and ahead of most of the day's postings.

The zone is pinned in code rather than configurable. Making it an env var only creates
a way to typo an identifier and raise on every tick forever. It is the operator's
clock, not the watched site's.

## Configuration

The library reads `DISCORD_WEBHOOK_URL`, `STATUS_WEBHOOK_URL`, `STATE_PATH`,
`POLL_INTERVAL_SEC`, `POLL_JITTER_PCT`, `HEARTBEAT_INTERVAL_SEC`, `HEARTBEAT_AT`, and
`STALL_ALERT_SEC`. Each app adds its own on top — jeffco `SFE_USER_ID`, `SFE_PIN`,
`HS_SCHOOLS`, `WINDOW_DAYS`; melanzana its calendar id and `MENTION_EVERYONE`.

`busy_stall_alert_sec` and `HTTP_TIMEOUT_SEC` are code constants, not env vars,
because nothing has yet needed to change them per environment. A variable nothing
consumes is config that silently does nothing.

Config is validated once at startup and refused loudly. A missing webhook exits
rather than polling forever with nowhere to report.

## Testing

```bash
uv run pytest -q                                  # 299
uv run mypy --strict lib apps tests tools
uv run ruff check . && uv run ruff format --check .
./tools/parity-diff.sh                            # needs node + the sibling repos
```

`pytest` covers the library and both apps against committed fixtures, on pinned
epochs — never a live clock, so a test cannot pass only until the first of the month.

**`parity-diff.sh` is the strongest check.** Both apps have a TypeScript reference
implementation in a sibling repo. The harness renders every Discord payload each
implementation can produce, on a frozen clock, as canonical JSON, and the diff must be
empty. It catches what unit tests miss: field ordering, number formatting, the
`allowed_mentions` pairing, and the exact en dash in a time range.

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
