# Python Monitor Pattern — Design

**Status:** approved design, not yet implemented.

## Goal

Establish a shared Python pattern for poll-and-alert monitors, and port
`melanzana-monitor` onto it as the first consumer. The pattern is validated
against `jeffco-sub-monitor`'s requirements so that porting jeffco later does not
force the library to be redesigned.

## Scope

**In scope**

- This repo (`monitors`), holding the shared library and the apps.
- `infra/` — the Terraform root, already moved here.
- `lib/monitor` — the shared library.
- `apps/melanzana` — melanzana ported from TypeScript to Python.
- Replacing konlet with systemd units for **both** monitors, with per-container
  memory caps and env files instead of plaintext instance metadata.

**Not in scope**

- Porting `jeffco-sub-monitor`'s code. Its container keeps running the existing
  TypeScript image; only its *supervision* changes. Its port gets its own design.
- The fashionjobs app. Its design waits on filter requirements from its user.
- Timers or any second execution model. Every app is a resident process.
- Docker Compose. It cannot run on this host — see [Why not Compose](#why-not-compose).
- An external dead-man's-switch. Recommended, but a separate change.

## Why Python

The deciding factor is app-specific rather than general: the fashionjobs source
sits behind Cloudflare bot protection that rejects ordinary HTTP clients on TLS
fingerprint. `curl_cffi` with `impersonate="chrome"` clears it — verified, HTTP
200, no cookies required. Node has no comparably mature equivalent. Python is
therefore the language the hardest of the three sources demands, and running one
language across all of them follows from that.

A secondary benefit is footprint. Measured on the host, the two Node containers
hold 104 MB and 98 MB. Python pollers of this shape typically land near 35 MB,
which would return roughly 130 MB of the 966 MB total. The 35 MB figure is an
expectation, not a measurement, and should be confirmed after the first deploy.

## Context: what is actually shared

Measured across the two existing TypeScript apps:

| Module | Divergence |
| --- | --- |
| `timing.ts` | byte-identical |
| `state.ts` | 4 lines, all doc comments |
| `health.ts` | jeffco's 4 exports are a strict subset of melanzana's 8 |
| `config.ts` | helpers shareable; the schema is per-app |
| `discord.ts` | `postAlert` shared; alert formatting genuinely per-app |
| `types.ts` | per-app |

The library covers state, timing, liveness, config primitives, webhook
transport, and ops-message formatting. Alert formatting stays in the app:
melanzana renders one embed with day-card fields, jeffco renders one message per
job.

## Repo layout

```
monitors/
├── pyproject.toml              uv workspace; lib and apps are members
├── infra/                      Terraform root for the shared host
├── lib/monitor/
│   ├── state.py                load_keys / save_keys
│   ├── timing.py               with_jitter / next_backoff
│   ├── health.py               HealthState / is_stalled / should_heartbeat
│   ├── config.py               env_num / env_str / env_bool / env_required / env_https_url
│   ├── discord.py              post() + heartbeat and status embeds
│   └── runner.py               run_forever()
└── apps/melanzana/
    ├── cowlendar.py            source client
    ├── alert.py                day-card embed
    └── main.py                 wires a Monitor to run_forever()
```

`infra/` holds the Terraform root for the shared host. See
[Terraform state](#terraform-state).

## Toolchain

- `uv` for dependencies and the workspace.
- Python 3.13 on `python:3.13-slim`. Not alpine: musl breaks some wheels, and
  `curl_cffi` will need manylinux when fashionjobs arrives.
- `pytest`, `ruff`, and `mypy --strict`.

## The Monitor contract

```python
Item = TypeVar("Item")           # app-defined; the library never inspects it


class Monitor(Protocol[Item]):
    async def fetch(self) -> list[Item]: ...          # may raise SourceBusy
    def key(self, item: Item) -> str: ...
    async def render(self, new: list[Item]) -> list[Message]: ...
    def heartbeat_fields(self) -> list[Field]: ...     # default: []


@dataclass(frozen=True)
class Message:
    payload: Payload
    covers: tuple[str, ...]      # the item keys this message announces
```

`Payload` and `Field` are library types mirroring Discord's webhook and embed-field
shapes. `Item` is whatever the app models — a slot, a job, a posting — and the
library only ever passes it back to `key` and `render`.

A class rather than free functions, because both apps hold per-instance state:
jeffco a token cache and an accumulated filter-gap set, melanzana none today.

`run_forever(monitor, cfg)` owns the loop: load state, fetch, key, diff,
suppress on first run, render, post with spacing, save, fold the outcome into
health, emit heartbeat and liveness messages, sleep with jitter or back off.

Three details in this contract exist because of jeffco specifically. Designing
from melanzana alone would have produced a library that jeffco could not use.

**`Message.covers`.** Jeffco posts one message per job and, when a post fails,
withholds exactly those jobs' keys from the baseline so the next tick re-alerts
them. The runner cannot do that without knowing which keys each message
announces. Melanzana's single message covers every fresh key, so the same shape
serves both.

**`heartbeat_fields`.** Jeffco's heartbeat carries the school names its filter
did not recognise, accumulated across the process lifetime. The library decides
*when* a heartbeat is due; the app supplies any extra content.

**`render` is async and may perform I/O.** Jeffco fetches per-job detail after
the diff to build each alert's date line, so enrichment is necessarily
post-diff.

`SourceBusy` is raised by a source to mean "the upstream is busy, not broken."
The runner retries at normal cadence instead of escalating backoff. Jeffco needs
this because SmartFindExpress answers HTTP 400 while the account holder's own
session is active: over the monitor's first ~59 hours all 34 of its 400s landed
between 06:00 and midnight, with none across three nights of overnight polling.
Escalating on those would blind the monitor for minutes precisely when someone
is claiming the job the alert just announced.

## Library boundaries

**The library owns** state persistence, the key-set diff, first-run
suppression, jitter and backoff, health folding, heartbeat and stall/recovery
ops messages, webhook transport, the status-webhook fallback to the alert
webhook, and inter-message spacing.

**The app owns** its source client, its key function, its alert rendering, its
filtering, and any authentication.

**Filtering is deliberately not in the library.** `fetch()` returns only the
items worth tracking, so jeffco's High-School predicate and its gap logging stay
inside jeffco. This keeps the contract at four methods instead of six.

**Authentication is deliberately not in the library.** Jeffco is the only app
with any: melanzana hits a public endpoint with no token, cookie, or session,
and the fashionjobs source needs no cookies at all. An auth abstraction would
have exactly one consumer, and the pieces that look generic are entangled with
one API — the expiry comes from a JWT `exp` claim, and the 120-second refresh
margin is a constant tuned to a 7200-second TTL. Jeffco's token cache, refresh
margin, and lockout guard live in its own `Monitor`.

`timing.py` carries the lockout hazard as a comment, because it is not obvious
until it bites:

> A wrong credential retried on the poll cadence is ~1,440 attempts a day. If
> the account belongs to a real person, that can cost them access to the thing
> the monitor exists to watch. Any authenticated source needs a failure ceiling,
> not just backoff.

## Post-failure semantics

A failed Discord post is an alerting problem, not a polling problem. The runner:

1. catches the failure,
2. withholds the affected message's `covers` keys from the saved baseline,
3. banks every other key,
4. keeps polling at normal cadence,
5. records the tick as a **success** for health purposes.

The next tick re-alerts the withheld items. With more than one message per tick
this can re-send an already-delivered message, since there is no per-message
bookkeeping. That trade is intended: a duplicate costs one glance, a swallowed
job can cost a day's work.

**This changes melanzana's behaviour, deliberately.** Melanzana currently lets a
post failure propagate out of the tick, which skips the state save, enters
backoff, and records a health failure — a path that drives a sustained Discord
outage toward a stall alert that would be delivered over the same broken
Discord. The port adopts jeffco's semantics, and the parity criteria carve this
out explicitly.

## Deployment

Both monitors run as systemd units. The startup script writes them to
`/etc/systemd/system/`, runs `daemon-reload`, and `enable --now`s them. Writing
them on every boot is idempotent and therefore correct whether or not `/etc`
persists.

```ini
[Unit]
After=docker.service
Requires=docker.service

[Service]
ExecStartPre=-/usr/bin/docker rm -f ${name}
ExecStart=/usr/bin/docker run --name ${name} \
  --memory=${memory} --log-opt max-size=10m --log-opt max-file=3 \
  --env-file /etc/monitors/${name}.env \
  --volume /var/lib/${name}-data:/data  ${image}
Restart=always
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=5
```

Rendered per app from one `templatefile()` over a committed app list, so adding
an app is a three-line diff:

```hcl
apps = {
  melanzana = { image_tag = "v1.2.0", memory = "128m" }
  jeffco    = { image_tag = "v1.1.0", memory = "256m" }
}
```

The host volume and env-file paths are derived from the app name
(`/var/lib/${name}-data`, `/etc/monitors/${name}.env`) rather than configured, so
there is one fewer thing to get wrong per app. Melanzana's existing data
directory already matches that convention.

- **Foreground `docker run`, and no `--restart=always`.** systemd is the
  supervisor; Docker's restart policy would be a second one contending with it.
- **No `--rm`.** An exited container costs no memory and keeps its logs readable
  until the next start, so `logs.sh` and Cloud Logging keep working unchanged.
- **Memory caps are leak backstops, not tuning knobs** — 128 MB for Python
  melanzana against ~35 MB expected, 256 MB for Node jeffco against 98.6 MB
  observed. Without a cap the kernel chooses the OOM victim, and there is no
  swap; with one, a leaking app dies instead of a healthy one.
- **`StartLimitBurst`** stops a crash loop from hammering an upstream.
- **Melanzana's secrets leave instance metadata.** Its Discord webhook currently
  sits in plaintext inside `gce-container-declaration`, readable by anyone with
  compute-viewer on the project. It moves to `/etc/monitors/melanzana.env` at
  `0600` inside a `0700` directory, matching jeffco.
- **Image tags become committed.** Tags are not secrets. They move to a
  committed `apps.auto.tfvars`; `terraform.tfvars` keeps only credentials. Today
  the deployed tag exists nowhere in git, so answering "what is running" requires
  SSH.
- **`deploy.sh`** runs `terraform apply`, then
  `google_metadata_script_runner startup` over SSH, then verifies both units are
  active. A metadata change does not re-run the startup script, so the second
  step is mandatory and easy to forget.

### Safety gates

- **konlet does not clean up after itself.** Removing
  `gce-container-declaration` leaves `klt-melanzana-monitor-*` running. The
  startup script must `docker rm -f` any container matching that prefix, or an
  orphan keeps 104 MB and double-posts alerts.
- **The Terraform plan must read `1 to change, 0 to destroy`.** This change
  removes a metadata key and the `container-vm` label, both of which should be
  in-place edits. A replace would wipe the boot disk and both `state.json`
  files, silently re-baselining both monitors.
- **Rollback** is reverting the tag in `apps.auto.tfvars` and running
  `deploy.sh`. Rehearse it once on melanzana before jeffco's unit is touched.

### Terraform state

State is a local file at `infra/terraform.tfstate`, alongside `terraform.tfvars`.
Both are gitignored; state stores secrets in plaintext, and losing it costs
melanzana's baseline, which would re-alert its entire backlog to a real channel.
Back it up outside the repo before any operation that touches it.

Only one Terraform root may exist. Two roots sharing one state means a stray
`apply` or `destroy` from the wrong directory is catastrophic.

### Why not Compose

`/usr/local/bin` does not exist on Container-Optimized OS, and `/var` — the only
writable filesystem — is mounted `noexec`. A Compose binary cannot execute on
this host. Enabling it would mean either remounting the stateful partition
`exec`, which disables a hardening control on a box holding a real district PIN,
or switching off COS and taking over OS patching. Compose also orchestrates
services that relate to one another; these three never communicate.

## Parity strategy

Melanzana's risk surface is small. **All date arithmetic is UTC** —
`getUTCFullYear`, `getUTCMonth`, `Date.UTC(..., 12)` — with no `Intl`, no local
time, and no DST, so the area where a language port most plausibly diverges is
absent. There is one fixture, `availability-sample.json`, at 663 bytes.

The health server is not ported. Production reports `health=off`, making
`startHealthServer`, `toSnapshot`, and `HealthSnapshot` dead code along with the
11 test assertions covering them. That takes the port from 1,455 lines and 61
tests to roughly 1,300 and 50.

Four layers, cheapest first:

1. **Reuse the fixture byte-for-byte.** Copy `availability-sample.json`
   unchanged; never regenerate it.
2. **Translate the surviving tests to pytest, tests before implementation.**
   They are the only written specification of which slots count as bookable, how
   day-cards group and sort, the `MAX_DAY_CARDS` overflow note, and first-run
   suppression.
3. **A differential harness.** Both implementations get a dump script that feeds
   the fixture with a frozen clock and prints the Discord payload as canonical
   JSON with sorted keys. `diff` must be empty. This compares the only output a
   user sees, and catches divergence unit tests miss: field ordering, number
   formatting, the `@everyone` content and `allowed_mentions` pair, the em-dash
   in `"14:00 — 2 left"`.
4. **A 48-hour shadow run.** Deploy the Python build against a scratch webhook at
   60 s rather than 10 s, so Cowlendar's request load is not doubled, and compare
   what each posts. Cowlendar needs no auth, so unlike SmartFindExpress there is
   no session-collision risk in running two.

Every parity test pins `nowUnix`, so month selection and weekday rendering are
deterministic rather than passing until the first of the month.

### Acceptance criteria

- ~50 pytest tests green; `mypy --strict` and `ruff` clean.
- Differential dump byte-identical on the fixture, with `MENTION_EVERYONE` both
  true and false.
- 48-hour shadow run posted the same alerts as production.
- Rollback exercised once.
- Post-failure behaviour asserted to match the new semantics, **not** melanzana's
  current behaviour. This is the one intentional divergence.

Not covered by parity: log line wording.

## Deferred

- Jeffco's code port — its own design cycle, and the harder one: America/Denver,
  DST, `Intl` offset extraction, 145 tests, and fixtures carrying real teacher
  names.
- The fashionjobs app, pending filter requirements from its user.
- `run_once()` and timers. Melanzana polls every 10 s, so a timer would mean
  ~8,640 container starts a day; every app is resident.
- Hoisting jeffco's lockout guard into `timing.py`, until a second app needs it.
- Moving Terraform into this repo.
- An external dead-man's-switch. A resident app reports its own inability to
  poll, but a dead process or a dead VM is invisible, and silence is
  indistinguishable from "nothing new."
- Renaming anything away from `melanzana-monitor`. The instance *can* be renamed
  in place — `gcloud compute instances set-name` on a stopped instance preserves
  the boot disk, so both baselines survive — but Terraform treats `name` as
  ForceNew, so it needs an out-of-band rename plus a state `rm` and `import`. It
  is also only a partial fix: the project ID is immutable and appears in every
  image path, every `logName`, and the service account address. The name is
  therefore fixed properly or not at all, and "properly" means a new project with
  Artifact Registry, IAM, images, and both monitors migrated. Do not propose an
  instance-only rename.
