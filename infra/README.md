# The monitors host

One always-on **`e2-micro`** on Container-Optimized OS in `cobs-cloud`, running
every monitor as its own container under systemd. Images come from Artifact
Registry; each app's `state.json` lives on a host-path volume so it survives
restarts and reboots.

**Cost:** ~**$3.60/mo**. The instance, its 30 GB disk, and Artifact Registry
storage are within the always-free tier; the only real line item is the external
IPv4 address (~$0.005/hr), needed for outbound calls. No inbound ports are opened.

> This repo owns the host even though it is named `monitors`. If you later deploy
> something here that is not a monitor, its Terraform still belongs in this
> `infra/`. **Only one Terraform root may exist for this host** — two roots sharing
> one state means a stray `apply` or `destroy` from the wrong directory is
> catastrophic.

## What it provisions

| Resource | Notes |
| --- | --- |
| `google_compute_instance.host` | named `monitors`, `e2-micro`, COS |
| `google_artifact_registry_repository.app` | one Docker repo per app, named after its key |
| `google_service_account.vm` + 3 IAM roles | pull images, write logs and metrics. Nothing else |

Per app, everything is derived from its key in `apps.auto.tfvars`: the container
and systemd unit are `<app>-monitor`, the data volume is `/var/lib/<app>-data`,
and the env file is `/etc/monitors/<app>.env` at `0600` inside a `0700` directory.
For FashionJobs that means `fashionjobs-monitor`, persistent host state at
`/var/lib/fashionjobs-data/state.json` mounted as `/data/state.json`, and the env file
`/etc/monitors/fashionjobs.env`.

## Supervision: systemd, not konlet

GCE's `gce-container-declaration` metadata (konlet) supervises **exactly one container
per instance**, so it cannot run this host. That metadata key and the `container-vm`
label are deliberately **not** set. Instead the startup script writes one systemd unit
per app, which is also what makes adding an app a three-line tfvars diff.

Consequences worth knowing:

- **No `--restart=always` on the containers.** systemd is the supervisor; two
  supervisors contending over one container behave in ways neither documents.
- **Container names are exact** (`<app>-monitor`), so log filters can match them
  exactly rather than by substring.
- **`--memory` per app** is a leak backstop, not a tuning knob — set well above
  normal operation. There is no swap, so without a cap the kernel picks the OOM
  victim, and it may pick a healthy monitor over the leaking one.
  FashionJobs adds a 128 MiB cap; the three declared caps are 128 MiB, 256 MiB, and
  128 MiB, or 512 MiB total. That fits the `e2-micro`'s roughly 1 GiB while leaving
  capacity for Container-Optimized OS, Docker, logging, and normal bursts.
- **Deploys are selective.** The startup script compares each rendered unit and
  env file against what is on disk and restarts only what changed, so deploying
  one app does not interrupt the others.
- **An image must create a uid-1000 user.** The startup script chowns
  `/var/lib/<app>-data` to `1000:1000`, and `python:3.13-slim` has no uid-1000 user of
  its own, so an image without one gets an unwritable `/data`. The library turns that
  into a per-tick log line rather than a crash, so it is quiet — the root `Dockerfile`
  creates the user, and a hand-rolled image is the first place to look. See
  [`docs/invariants.md`](../docs/invariants.md).
- **One app's missing image does not skip the others.** The per-app blocks are a
  Terraform template loop that unrolls into sequential bash, so an `exit` in one would
  leave the whole script — and every app sorting after it would keep its old env file,
  its old unit, and no restart. A failing app is recorded and skipped instead, and the
  non-zero exit comes after the restart loop.

## Deploying

```bash
cd infra
./deploy.sh
```

That is the whole thing, and using it matters: **`terraform apply` alone is only
half a deploy.** GCE does not re-run the startup script when metadata changes, so
applying on its own leaves the host running exactly what it ran before — a deploy
that looks like it worked and didn't. `deploy.sh` applies, re-runs the startup
script over SSH, then verifies units, containers, memory, and recent output.

It also **refuses any plan containing a delete**, because a replaced instance
takes the boot disk with it, along with every app's `state.json`.

To ship a new version, bump the tag in `apps.auto.tfvars` (committed, so git
records what is deployed) and run `./deploy.sh`.

`<app>_heartbeat_at` (default `07:00`) fixes that app's heartbeat to an America/Denver
wall-clock hour. It is wired conditionally, so leaving it empty falls back to
`HEARTBEAT_INTERVAL_SEC` rather than passing an empty string — those are different
behaviours.

FashionJobs is configured to poll every 600 seconds; the shared runner applies its
default 20 percent jitter. Its daily heartbeat is configured for `07:00`
America/Denver.

Each FashionJobs process startup performs one transactional full walk through the
declared pagination range, capped at 100 pages. A successful process performs another
full safety walk every 86,400 monotonic seconds; ordinary ticks between them normally
fast-stop at page 1, or about six page-1 requests per hour. An ordinary frontier scan
continues to later pages whenever each page still introduces unseen IDs; that does
not require an earlier failure. Failed startup or due walks remain due and retry from
page 1 without advancing identity state. Plan network traffic as the ordinary six
page-1 requests per hour, any pages required by newly advancing frontiers, plus up to
one declared full walk daily and one per restart. Failed required scans add retries.

```bash
./deploy.sh --plan      # plan only
./deploy.sh --verify    # verify only, change nothing
```

## First-time setup

```bash
gcloud auth login
gcloud auth application-default login    # Terraform uses these
gcloud config set project cobs-cloud
gcloud services enable compute.googleapis.com artifactregistry.googleapis.com
cp terraform.tfvars.example terraform.tfvars   # then fill in the credentials
terraform init
```

Set `fashionjobs_discord_webhook_url` in the gitignored `terraform.tfvars`. The
optional `fashionjobs_status_webhook_url` uses a separate ops channel when supplied
and otherwise falls back to the alert webhook.

The registries must exist before the host can pull anything, so create them
first:

```bash
terraform apply -target=google_artifact_registry_repository.app
```

Then build and push each image. **On an ARM Mac you must build for
`linux/amd64`** — the `e2-micro` is x86, and a native arm64 image fails on the
host with "exec format error":

Every app builds from the one root `Dockerfile` with `--build-arg APP`, from this repo's
root. The image name and tag come from `apps.auto.tfvars`:

```bash
REGION=us-west1
PROJECT=cobs-cloud
gcloud auth configure-docker "$REGION-docker.pkg.dev"

for app in melanzana jeffco fashionjobs; do
  # image name and tag must match this app's entry in apps.auto.tfvars
  IMAGE="$REGION-docker.pkg.dev/$PROJECT/$app/<image>:<tag>"
  docker build --platform linux/amd64 --build-arg "APP=$app" -t "$IMAGE" .
  docker push "$IMAGE"
done
```

### FashionJobs rollout status

The first FashionJobs `v2.1.0` rollout proved the production GCE egress path and
Discord webhook, then failed closed because the live page omitted optional localized
timestamp display text while retaining the required absolute `data-value`. A
one-page `v2.1.1` smoke then failed closed because titled recruitment links surround
and otherwise overwrite the real job link. Both published images remain immutable;
`v2.1.2` contains the focused fixes and is the current tag in `apps.auto.tfvars`.

From the repository root, the FashionJobs image step is:

```bash
IMAGE="us-west1-docker.pkg.dev/cobs-cloud/fashionjobs/fashionjobs-monitor:v2.1.2"
docker build --platform linux/amd64 --build-arg APP=fashionjobs -t "$IMAGE" .
docker push "$IMAGE"
```

The verified request profile is the app's configured `Accept: text/html` and
`User-Agent: fashionjobs-monitor/2.0`; an unrepresentative default-httpx probe was
rejected with HTTP 403 before the exact production profile returned HTTP 200.

If a build hangs with no output at all, the credential helper is stuck rather than the
build being slow: every registry operation that consults credentials blocks, including
resolving a *public* base image. `pkill -f docker-credential-desktop` and retry.

Then `./deploy.sh` for the rest.

## Reading the logs

Every container's stdout lands in one `cos_containers` log, so the Logs Explorer
interleaves them. `logs.sh` applies the right filter:

```bash
./logs.sh jeffco                            # newest 50 in the last hour
./logs.sh melanzana --freshness=6h
./logs.sh fashionjobs --freshness=6h
./logs.sh jeffco --limit=200 --order=asc    # oldest first
```

Timestamps render in Denver time. This reaches further back than `docker logs`,
which only holds the current container.

> **Do not filter by typing an app's name into the search box.** Bare text search
> matches `logName` too, so it returns the guest agent, the audit log, and every
> other app at once. Scope it to the container name, or use `logs.sh`.

```
logName="projects/cobs-cloud/logs/cos_containers"
jsonPayload."cos.googleapis.com/container_name"="jeffco-monitor"
```

**The container logs do not say which version produced them.** A `cos_containers` entry
carries only `container_id`, `container_name`, `stream` and `message` — no image, no tag.
The image *is* in `cos_system`'s container-start events, so a per-app query has to union
the two streams to show both what an app said and what version said it:

```
(
  logName="projects/cobs-cloud/logs/cos_containers"
  AND jsonPayload."cos.googleapis.com/container_name"="jeffco-monitor"
)
OR
(
  logName="projects/cobs-cloud/logs/cos_system"
  AND jsonPayload.MESSAGE:"container start"
  AND jsonPayload.MESSAGE:"jeffco-sub-monitor:v"
  AND -jsonPayload.MESSAGE:"exec"
)
```

Interleaved, that reads as the deploy narrative: `SIGTERM received` → `container start
(image=…:v2.0.2)` → `app config` → `started … firstRun=False`.

Two details bite. **The container name and the image name differ for jeffco** — container
`jeffco-monitor`, image `jeffco-sub-monitor` — so the two halves cannot share one string.
And **`-exec` is load-bearing**: an ad-hoc `docker exec` into a container also logs the
image, so without it one memory probe buries the real deploys (measured: 295 matching
entries against 78).

Exactly two saved queries exist in Logs Explorer (project `cobs-cloud`, location
`global`) — `melanzana-monitor-logs` and `jeffco-monitor-logs`, one per app, each
carrying the union above. There is deliberately no third, cross-app query.

`--order=asc` is handled by the script rather than passed through, because
`gcloud logging read` **ignores `--freshness` when asked for ascending order** — it
returns the oldest entries in the whole retention window, so a 15-minute query
answers with days-old lines and gives no sign anything went wrong. The script
queries newest-first and reverses the rows.

Container logs rotate at 10 MB × 3 per app, on the 26 GB stateful partition. The
~65% that `df -h /` reports is COS's fixed 2 GB read-only system partition;
container logs and images are not on it.

## On-host inspection

```bash
gcloud compute ssh monitors --zone us-west1-b
systemctl status jeffco-monitor
journalctl -u jeffco-monitor -n 50
systemctl status fashionjobs-monitor
journalctl -u fashionjobs-monitor -n 50
docker ps
```

## State and safety

`terraform.tfstate` is local and holds every webhook and the SFE PIN in
plaintext, so it is gitignored. **Back it up outside the repo before anything
that touches it** — losing it costs each monitor's baseline.

Losing an app's `state.json` is survivable: the next run records a fresh silent
baseline, so nothing is falsely alerted. It does mean everything currently open
goes unannounced, which is usually what you want. `echo '[]' > state.json` is the
supported way to ask for the current backlog instead.

State JSON arrays must contain strings. FashionJobs additionally accepts only
canonical positive-decimal IDs that safely round-trip through integer conversion.
Any invalid element marks the whole file corrupt; startup logs that loss loudly,
performs its full scan, and writes a fresh silent baseline rather than partially
trusting or coercing the file.

For FashionJobs, inspect the persistent state file on the host without changing it:

```bash
sudo ls -l /var/lib/fashionjobs-data/state.json
```
