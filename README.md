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

Design only. Nothing here is implemented yet. Start with
[the design](docs/superpowers/specs/2026-08-20-python-monitor-pattern-design.md).

`melanzana-monitor` and `jeffco-sub-monitor` still live in their own repos and
still run in production from there; `melanzana-monitor/infra` remains the
Terraform root for the shared host.
