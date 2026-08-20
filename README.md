# monitors

Poll-and-alert monitors that watch a website and post to Discord when something
new appears. They share a small Python library and run as containers on a single
free-tier GCE `e2-micro`.

| App | Watches | Language |
| --- | --- | --- |
| `melanzana` | Melanzana appointment slots (Cowlendar) | TypeScript → Python |
| `jeffco` | Jeffco substitute teaching jobs (SmartFindExpress) | TypeScript |
| `fashionjobs` | fr.fashionjobs.com postings | planned |

Every app implements four methods — `fetch`, `key`, `render`,
`heartbeat_fields` — and the shared `run_forever()` owns the poll loop, state
diffing, backoff, health, and Discord delivery.

## Status

`infra/` is live: it is the Terraform root for the shared host, and both
monitors are deployed from here. The app code has not moved yet —
`melanzana-monitor` and `jeffco-sub-monitor` still live in their own repos and
run from images built there.

The shared library and the Python ports are designed but not implemented. Start
with [the design](docs/superpowers/specs/2026-08-20-python-monitor-pattern-design.md).

## Deploying

```bash
cd infra
terraform plan          # must never propose destroy or replace
terraform apply
```

A metadata change does **not** re-run the instance startup script, so applying
is only half a deploy — see the runbook in [`infra/README.md`](infra/README.md).
Read logs with `./infra/logs.sh jeffco` or `./infra/logs.sh melanzana`.
