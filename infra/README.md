# Deploying melanzana-monitor to GCP (Terraform)

Runs the monitor as an always-on container on a single **`e2-micro`**
Container-Optimized OS VM in a GCP free-tier region. The container is pulled
from **Artifact Registry**; `state.json` lives on a host-path volume so it
survives container restarts and VM reboots.

**Cost:** ~**$3.60/mo** — the `e2-micro`, its 30 GB disk, and Artifact Registry
storage are within the always-free tier; the only real line item is the
external IPv4 address (~$0.005/hr), needed for outbound calls to Cowlendar and
Discord. No inbound ports are opened.

> The Discord webhook is passed as a container **env var**, so it lands in
> Terraform state and instance metadata in plaintext. `terraform.tfvars` and
> `*.tfstate` are gitignored — keep them off version control. Use a webhook you
> can rotate if it leaks.

---

## Prerequisites

- A **personal** GCP project with billing enabled (not a work project).
- [`gcloud`](https://cloud.google.com/sdk/docs/install) and
  [`terraform`](https://developer.hashicorp.com/terraform/install) installed.
- Docker (Desktop or engine) running locally.

```bash
gcloud auth login
gcloud auth application-default login   # Terraform uses these credentials
gcloud config set project YOUR_PROJECT_ID
```

Enable the APIs Terraform needs:

```bash
gcloud services enable \
  compute.googleapis.com \
  artifactregistry.googleapis.com
```

---

## 1. Configure

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: set project_id, discord_webhook_url, image_tag.
terraform init
```

## 2. Create the Artifact Registry repo first

The VM can't start until the image exists, so create just the repo before
building:

```bash
terraform apply -target=google_artifact_registry_repository.repo
```

## 3. Build & push the image

> **ARM Macs:** you must build for `linux/amd64` — the `e2-micro` is x86. A
> native arm64 image fails on the VM with "exec format error".

From the **repo root** (one level up from `infra/`):

```bash
REGION=us-west1            # match your tfvars
PROJECT=YOUR_PROJECT_ID
TAG=v1.1.0                 # match image_tag in terraform.tfvars
IMAGE="$REGION-docker.pkg.dev/$PROJECT/melanzana/melanzana-monitor:$TAG"

gcloud auth configure-docker "$REGION-docker.pkg.dev"

docker build --platform linux/amd64 -t "$IMAGE" .
docker push "$IMAGE"
```

## 4. Deploy the VM

```bash
cd infra
terraform apply
```

Terraform prints the instance name, zone, and external IP.

## 5. Verify it's running

```bash
# Tail the container logs over SSH (look for the "[melanzana-monitor] started" line):
gcloud compute ssh melanzana-monitor --zone us-west1-b \
  --command 'docker logs $(docker ps -q --filter name=melanzana-monitor) --tail 50 -f'
```

First run logs `firstRun=true` and records a silent baseline (no alert). New
slots appearing on later ticks trigger the Discord alert.

If you set `health_port`, check it on-box (it is not exposed publicly):

```bash
gcloud compute ssh melanzana-monitor --zone us-west1-b \
  --command 'curl -s -i http://127.0.0.1:8080/'
```

---

## Reading the logs

Two containers share this VM, and Container-Optimized OS funnels both stdout
streams into a single `cos_containers` log — so the Logs Explorer interleaves
them with nothing obvious to filter on. `logs.sh` applies the right filter:

```bash
cd infra
./logs.sh jeffco                            # jeffco-sub-monitor
./logs.sh melanzana --freshness=6h          # melanzana-monitor
./logs.sh jeffco --limit=200 --order=asc    # oldest first
```

Timestamps render in Denver time. Extra flags pass through to `gcloud logging
read`, and yours win over the defaults (`--freshness=1h --limit=50`, newest
first). Set `PROJECT=` to target a project other than gcloud's current one.

`--order=asc` is handled by the script rather than passed through, because
`gcloud logging read` ignores `--freshness` when asked for ascending order — it
returns the oldest entries in the whole retention window, so a 15-minute query
answers with three-day-old lines and gives no sign anything went wrong. The
script queries newest-first, where freshness works, and reverses the rows.

This reaches further back than `docker logs`, which only holds the **current**
container — Cloud Logging keeps the earlier ones, so history survives a
redeploy.

In the Console, use the **Saved queries** tab in the Logs Explorer — there is one
per monitor (`Monitor — jeffco`, `Monitor — melanzana`), which is a click rather
than a paste.

> **Do not filter by typing a monitor's name into the search box.** The project
> is itself named `melanzana-monitor`, so that string is in `logName` on *every*
> entry: a bare search for it returns the guest agent, the audit log, and both
> monitors' lines at once. Measured over one 12-hour window it matched 300
> entries, only 3 of which were melanzana's. Scope it to the message instead —
> `jsonPayload.message:"melanzana-monitor"` — or use a saved query.

The saved queries hold these filters, if you need to rebuild them:

```
logName="projects/YOUR_PROJECT_ID/logs/cos_containers"
jsonPayload."cos.googleapis.com/container_name"="jeffco-monitor"
```

For melanzana, match a **substring**. konlet names its container
`klt-melanzana-monitor-<random>` and regenerates that suffix whenever it
recreates the container, so an exact match will one day quietly return nothing:

```
jsonPayload."cos.googleapis.com/container_name":"melanzana-monitor"
```

### Rotation

| Container | Cap | Set by |
| --- | --- | --- |
| `jeffco-monitor` | 10 MB × 3 | `--log-opt` in the startup script |
| `klt-melanzana-monitor-*` | 500 MB × 3 | konlet, on its own |

Both live on the 26 GB stateful partition. The ~65% that `df -h /` reports is
COS's fixed 2 GB read-only system partition; container logs and images are not
on it.

---

## Updating to a new image

Preferred — preserves `state.json` (no re-baseline). Bump `image_tag` in
`terraform.tfvars` (e.g. to `v1.1.1`), then update the container declaration
in place and reset the VM so COS re-pulls:

```bash
# 1. Build & push the new tag (step 3) with TAG=v1.1.1
# 2. Update the instance metadata in place (boot disk, and state, untouched):
terraform apply
# 3. Reboot so Container-Optimized OS re-reads the declaration and pulls it:
gcloud compute instances reset melanzana-monitor --zone us-west1-b
```

After it reboots, the startup log shows `firstRun=false` — confirming the
existing baseline survived.

> Avoid `terraform apply -replace='google_compute_instance.monitor'` for routine
> updates: it recreates the VM and wipes the boot disk, so `state.json` is lost
> and the next run records a fresh silent baseline.

## Tearing down

```bash
terraform destroy
```

Removes the VM, IP, service account, and Artifact Registry repo.

> **State note:** `state.json` lives on the VM's boot disk. `terraform destroy`
> or recreating the instance wipes it; the next run just records a fresh silent
> baseline (no false alerts). A **stop/start or reboot keeps it** — only
> deletion loses it.
