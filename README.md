# monitors

Poll-and-alert monitors that watch a website and post to Discord when something new
appears. They share a small Python library and run as containers on a single free-tier
GCE `e2-micro`.

| App | Watches | Source |
| --- | --- | --- |
| `melanzana` | Melanzana appointment slots | Cowlendar |
| `jeffco` | Jeffco substitute teaching jobs | SmartFindExpress |
| `fashionjobs` | fr.fashionjobs.com postings | planned |

Every app implements four methods — `fetch`, `key`, `render`, `heartbeat_extras` — and
the shared `run_forever()` owns the poll loop, state diffing, backoff, health, and
Discord delivery.

- [`docs/architecture.md`](docs/architecture.md) — how it is built and why
- [`docs/invariants.md`](docs/invariants.md) — **rules that look removable and are not.** Read before simplifying anything
- [`apps/README.md`](apps/README.md) — adding an app
- [`infra/README.md`](infra/README.md) — the host, deploys, logs

## Working on it

```bash
uv sync
uv run pytest -q                                   # 299
uv run mypy --strict lib apps tests tools
uv run ruff check . && uv run ruff format --check .
./tools/parity-diff.sh                             # needs node + the sibling repos
```

Both apps have a TypeScript reference implementation in a sibling repo.
`parity-diff.sh` renders every Discord payload each implementation can produce on a
frozen clock and requires the diff to be empty — it catches field ordering, number
formatting and exact punctuation that unit tests miss. It is outside `pytest` because it
needs those repos and a node toolchain.

## Design decisions worth knowing up front

Each of these exists because the simpler version silently misses an alert. The full list,
with the test that holds each one, is in [`docs/invariants.md`](docs/invariants.md).

1. **A failed Discord post does not stop the tick.** The runner withholds that message's
   keys from the baseline, banks the rest, keeps polling, and counts the tick healthy. A
   duplicate costs one glance; a swallowed item can cost a day's work.
2. **A failed state write is logged, not reported as a poll failure.** Reporting it as one
   would throttle polling, latch a false death alert, and suppress the heartbeat — while
   alerts were arriving fine.
3. **Fresh items are de-duplicated by key**, in the runner, for every app.
4. **A busy source is not a broken source.** `SourceBusy` holds the poll cadence instead
   of escalating backoff, but still counts as an absence of data, so a permanently-busy
   source alerts eventually rather than going quiet forever.
5. **`HEARTBEAT_AT`** fixes the heartbeat to an America/Denver wall-clock time instead of
   drifting with process start. Both apps run `07:00`.

## Deploying

```bash
./infra/deploy.sh
```

That is the whole thing, and using it matters: **`terraform apply` alone is only half a
deploy.** GCE does not re-run the startup script when instance metadata changes, so
applying on its own leaves the host running exactly what it ran before. `deploy.sh`
applies, re-runs the startup script over SSH, verifies every unit, and **refuses any plan
containing a delete** — a replaced instance takes the boot disk with it, and every app's
`state.json`.

```bash
./infra/deploy.sh --plan      # plan only
./infra/deploy.sh --verify    # verify only, change nothing
```

To ship a new version, bump the tag in `infra/apps.auto.tfvars` (committed, so git records
what is deployed) and run `./infra/deploy.sh`. Rolling back is the same edit in reverse.
The full runbook is in [`infra/README.md`](infra/README.md).

Logs are per app:

```bash
./infra/logs.sh melanzana
./infra/logs.sh jeffco --freshness=6h
```

Both apps log nothing on a tick with no news, so a quiet log is normal — and means a hung
loop looks the same as an idle one.
