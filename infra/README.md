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
> `infra/` — the whole reason this directory exists is that the host used to be
> provisioned from inside one of its tenants.

## What it provisions

| Resource | Notes |
| --- | --- |
| `google_compute_instance.host` | named `monitors`, `e2-micro`, COS |
| `google_artifact_registry_repository.app` | one Docker repo per app, named after its key |
| `google_service_account.vm` + 3 IAM roles | pull images, write logs and metrics. Nothing else |

Per app, everything is derived from its key in `apps.auto.tfvars`: the container
and systemd unit are `<app>-monitor`, the data volume is `/var/lib/<app>-data`,
and the env file is `/etc/monitors/<app>.env` at `0600` inside a `0700` directory.

## Supervision: systemd, not konlet

GCE's `gce-container-declaration` metadata (konlet) supervises **exactly one
container per instance**, which is why this host used to run one monitor under
konlet and the other from a hand-rolled `docker run` in the startup script. That
metadata key and the `container-vm` label are deliberately **not** set here.
Instead the startup script writes one systemd unit per app.

Consequences worth knowing:

- **No `--restart=always` on the containers.** systemd is the supervisor; two
  supervisors contending over one container behave in ways neither documents.
- **Container names are stable.** konlet generated `klt-<name>-<random>` and
  changed the suffix on every recreate, which is why log filters used to need a
  substring match. Exact matches are safe now.
- **`--memory` per app** is a leak backstop, not a tuning knob — set well above
  normal operation. There is no swap, so without a cap the kernel picks the OOM
  victim, and it may pick a healthy monitor over the leaking one.
- **Deploys are selective.** The startup script compares each rendered unit and
  env file against what is on disk and restarts only what changed, so deploying
  one app does not interrupt the others.

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

The registries must exist before the host can pull anything, so create them
first:

```bash
terraform apply -target=google_artifact_registry_repository.app
```

Then build and push each image. **On an ARM Mac you must build for
`linux/amd64`** — the `e2-micro` is x86, and a native arm64 image fails on the
host with "exec format error":

```bash
REGION=us-west1
PROJECT=cobs-cloud
gcloud auth configure-docker "$REGION-docker.pkg.dev"

# from the melanzana-monitor repo root
docker build --platform linux/amd64 \
  -t "$REGION-docker.pkg.dev/$PROJECT/melanzana/melanzana-monitor:v1.2.0" .
docker push "$REGION-docker.pkg.dev/$PROJECT/melanzana/melanzana-monitor:v1.2.0"

# from the jeffco-sub-monitor repo root
docker build --platform linux/amd64 \
  -t "$REGION-docker.pkg.dev/$PROJECT/jeffco/jeffco-sub-monitor:v1.1.0" .
docker push "$REGION-docker.pkg.dev/$PROJECT/jeffco/jeffco-sub-monitor:v1.1.0"
```

Then `./deploy.sh` for the rest.

## Reading the logs

Every container's stdout lands in one `cos_containers` log, so the Logs Explorer
interleaves them. `logs.sh` applies the right filter:

```bash
./logs.sh jeffco                            # newest 50 in the last hour
./logs.sh melanzana --freshness=6h
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

Only one Terraform root may exist for this host. Two roots sharing one state means
a stray `apply` or `destroy` from the wrong directory is catastrophic.
