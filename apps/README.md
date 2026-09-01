# Adding an app

The library owns the loop. An app owns its source, its key, its alert rendering,
its filtering, and its authentication. Four methods — see
`lib/monitor/src/monitor/types.py`, the `Monitor` protocol.

FashionJobs is the clearest example of that boundary. Its app-owned source parses the
fixed France-wide `Stage` HTML route, proves the selected filter and required card and
pagination structure on every page, and applies no role or keyword filter. Its numeric
FJOB ID is the key. When an older listing is no longer present in the current HTML, the
source returns a `KnownJob` identity placeholder so the shared runner's baseline stays
monotonic. None of this adds methods to the four-method shared contract.

**Order matters.** The registry repository is created by Terraform from the app
list, so the tfvars entry comes before the image push, and the push comes before
the deploy — otherwise the startup script finds no image.

1. `apps/<app>/pyproject.toml` — copy melanzana's. Name it `<app>`; depend on
   `monitor` (`{ workspace = true }`) plus whatever HTTP client the source needs.
2. Root `pyproject.toml` — add `apps/<app>` to `[tool.uv.workspace] members`.
3. `just setup` — regenerates `uv.lock`. The Dockerfile needs the lock committed.
4. `apps/<app>/src/<app>/` — `types.py`, the source client, `alert.py`,
   `config.py`, `monitor.py`, `main.py`. Copy melanzana's `main.py` and change the
   two constructor calls; it is the one file that is meant to be per-app, because
   it is where the HTTP client is chosen. Keep parsing and fixed product filters in
   the app: FashionJobs owns its HTML parser, exact `Stage` filter, pagination
   validation, and retained identity placeholders.
5. `tests/<app>/` — the source client, the renderer, the config, the four methods.
6. `infra/variables.tf` — one `sensitive = true` variable per credential, plus
   `<app>_heartbeat_at` if it should report at a fixed hour.
7. `infra/main.tf` — a `local.app_env.<app>` map. The instance has a precondition
   that fails the plan if this is missing, so a forgotten entry cannot ship.
8. `infra/apps.auto.tfvars` — the `apps` entry: `image`, `image_tag`, `memory`.
9. `infra/terraform.tfvars` — the secrets (gitignored, never committed).
10. `cd infra && terraform apply -target=google_artifact_registry_repository.app`
    — creates the repository so there is somewhere to push.
11. Build and push. **`--platform linux/amd64`**: the host is x86 and an arm64
    image fails with "exec format error". Configure the registry credential helper
    first — without it the push fails with an auth error that reads like a
    permissions problem.

    ```bash
    REGION=us-west1
    PROJECT=cobs-cloud
    gcloud auth configure-docker "$REGION-docker.pkg.dev"

    IMAGE="$REGION-docker.pkg.dev/$PROJECT/<app>/<image>:<tag>"
    docker build --platform linux/amd64 --build-arg APP=<app> -t "$IMAGE" .
    docker push "$IMAGE"
    ```
12. `./infra/deploy.sh` — applies, re-runs the startup script, verifies. Never a
    bare `terraform apply`.
13. Check the first log lines: `just logs <app> --freshness=10m`.

## The three things that fail quietly

- **`firstRun=true` when you expected `false`.** The app did not read an existing
  baseline, so everything currently open goes unannounced. Nothing looks wrong.
- **A state-write failure.** `/data` unwritable logs one line per tick while
  alerts keep arriving. The root `Dockerfile` creates the uid-1000 user that avoids
  this; a hand-rolled image is the first place to look.
- **A missing `HEARTBEAT_AT`.** The heartbeat silently reverts to anchoring on
  process start, so it arrives at whatever time the last deploy happened.

## What you do NOT write

State persistence, the key-set diff, first-run suppression, jitter, backoff,
`SourceBusy` cadence handling, health folding, heartbeat and stall/recovery
messages, the status-webhook fallback, inter-message spacing, post-failure
withholding, or Discord transport. If you are writing one of those, check whether
`lib/monitor` already has it.
