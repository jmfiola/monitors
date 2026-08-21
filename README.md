# monitors

Poll-and-alert monitors that watch a website and post to Discord when something
new appears. They share a small Python library and run as containers on a single
free-tier GCE `e2-micro`.

| App | Watches | Language |
| --- | --- | --- |
| `melanzana` | Melanzana appointment slots (Cowlendar) | Python |
| `jeffco` | Jeffco substitute teaching jobs (SmartFindExpress) | Python |
| `fashionjobs` | fr.fashionjobs.com postings | planned |

Every app implements four methods — `fetch`, `key`, `render`,
`heartbeat_extras` — and the shared `run_forever()` owns the poll loop, state
diffing, backoff, health, and Discord delivery.

## Status

`infra/` is live: it is the Terraform root for the host, which runs in the
**`cobs-cloud`** project as an instance named `monitors`. Both apps run there as
systemd units.

`lib/monitor` and `apps/melanzana` are implemented in Python. `jeffco` still runs
the TypeScript image built in its own repo; its port has its own design cycle.

The reasoning behind the library's shape is in
[the design](docs/superpowers/specs/2026-08-20-python-monitor-pattern-design.md);
adding an app is [`apps/README.md`](apps/README.md).

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
3. Fresh items are de-duplicated by key. Melanzana's month enumeration pads and
   therefore overlaps, so one slot returned by two month queries used to alert as
   "2 open slot(s)" with its day-card line repeated. The library de-duplicates in
   `run_tick`, fixing it for every app.
4. A shape-valid but impossible date (`2026-02-30`) renders differently. The
   TypeScript's `Date` rolls it over and prints a header for the wrong day; Python
   falls back to the raw key instead of raising, so a render failure can't silence
   all alerting. Deliberately excluded from the differential harness — the two
   implementations differ here by design, so a unit test is the only guard.

`HEARTBEAT_AT` is new: unset it behaves exactly as before, set to `HH:MM` the
heartbeat lands at that America/Denver wall-clock time instead of drifting with
process start. Production runs `07:00`.

## Deploying

```bash
./infra/deploy.sh
```

That is the whole thing, and using it matters: **`terraform apply` alone is only
half a deploy.** GCE does not re-run the startup script when instance metadata
changes, so applying on its own leaves the host running exactly what it ran
before. `deploy.sh` applies, re-runs the startup script over SSH, verifies both
units, and **refuses any plan containing a delete** — a replaced instance takes
the boot disk with it, and every app's `state.json`.

```bash
./infra/deploy.sh --plan      # plan only
./infra/deploy.sh --verify    # verify only, change nothing
```

To ship a new version, bump the tag in `infra/apps.auto.tfvars` (committed, so
git records what is deployed) and run `./infra/deploy.sh`. The full runbook is in
[`infra/README.md`](infra/README.md).

Logs are per app:

```bash
./infra/logs.sh melanzana
./infra/logs.sh jeffco --freshness=6h
```
