# monitors

Poll-and-alert monitors that watch a website and post to Discord when something new
appears. They share a small Python library and run as containers on a single free-tier
GCE `e2-micro`.

| App | Watches | Source |
| --- | --- | --- |
| `melanzana` | Melanzana appointment slots | Cowlendar |
| `jeffco` | Jeffco substitute teaching jobs | SmartFindExpress |
| `fashionjobs` | France-wide `Stage` listings, all roles and no keywords | [fixed FashionJobs HTML route](https://fr.fashionjobs.com/fr/contrat/Stage,5.html) |

Every app implements four methods — `fetch`, `key`, `render`, `heartbeat_extras` — and
the shared `run_forever()` owns the poll loop, state diffing, backoff, health, and
Discord delivery.

- [`docs/architecture.md`](docs/architecture.md) — how it is built and why
- [`docs/invariants.md`](docs/invariants.md) — **rules that look removable and are not.** Read before simplifying anything
- [`apps/README.md`](apps/README.md) — adding an app
- [`infra/README.md`](infra/README.md) — the host, deploys, logs

## Working on it

The `justfile` is the command interface (`brew install just`); `just --list` shows every
recipe.

```bash
just setup                                # install the uv workspace
just check                                # test, typecheck, lint, format-check
just test tests/fashionjobs -k pagination # forwards pytest args
```

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
   drifting with process start. All three apps are configured for `07:00`.

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
just infra-plan      # plan only
just infra-verify    # verify only, change nothing
```

To ship a new version, bump the tag in `infra/apps.auto.tfvars` (committed, so git records
what is deployed) and run `./infra/deploy.sh`. Rolling back is the same edit in reverse.
The full runbook is in [`infra/README.md`](infra/README.md).

Logs are per app:

```bash
just logs melanzana
just logs jeffco --freshness=6h
just logs fashionjobs --freshness=6h
```

Apps log nothing on a tick with no news, so a quiet log is normal — and means a hung
loop looks the same as an idle one.
