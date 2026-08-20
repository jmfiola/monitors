###############################################################################
# Artifact Registry — holds the melanzana-monitor container image.
# Build & push to it (see infra/README.md) before `terraform apply`.
###############################################################################
resource "google_artifact_registry_repository" "repo" {
  location      = var.region
  repository_id = "melanzana"
  format        = "DOCKER"
  description   = "melanzana-monitor container images"
}

###############################################################################
# Dedicated service account for the VM. Least-privilege: pull images, write
# logs/metrics. No inbound access, no other cloud permissions.
###############################################################################
resource "google_service_account" "vm" {
  account_id   = "melanzana-monitor-vm"
  display_name = "melanzana-monitor VM"
}

resource "google_project_iam_member" "artifact_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_project_iam_member" "log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_project_iam_member" "metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

###############################################################################
# Container-Optimized OS boot image (auto-patching, Docker preinstalled).
###############################################################################
data "google_compute_image" "cos" {
  family  = "cos-stable"
  project = "cos-cloud"
}

locals {
  image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.repo.repository_id}/melanzana-monitor:${var.image_tag}"

  # Always-set env. STATE_PATH points at the mounted /data volume so state
  # survives container restarts and VM reboots.
  base_env = [
    { name = "DISCORD_WEBHOOK_URL", value = var.discord_webhook_url },
    { name = "STATE_PATH", value = "/data/state.json" },
    { name = "WINDOW_DAYS", value = tostring(var.window_days) },
    { name = "MENTION_EVERYONE", value = tostring(var.mention_everyone) },
    { name = "HEARTBEAT_INTERVAL_SEC", value = tostring(var.heartbeat_interval_sec) },
    { name = "STALL_ALERT_SEC", value = tostring(var.stall_alert_sec) },
  ]

  # Only included when set — keeps "unset = v1 behavior" intact.
  optional_env = concat(
    var.status_webhook_url != "" ? [{ name = "STATUS_WEBHOOK_URL", value = var.status_webhook_url }] : [],
    var.health_port != 0 ? [{ name = "HEALTH_PORT", value = tostring(var.health_port) }] : [],
  )

  # GCE container declaration consumed by COS. hostPath mount keeps state.json
  # on the boot disk at /var/lib/melanzana-data (created by the startup script).
  container_spec = {
    spec = {
      containers = [{
        name  = "melanzana-monitor"
        image = local.image
        env   = concat(local.base_env, local.optional_env)
        volumeMounts = [{
          name      = "data"
          mountPath = "/data"
          readOnly  = false
        }]
        stdin = false
        tty   = false
      }]
      volumes = [{
        name = "data"
        hostPath = {
          path = "/var/lib/melanzana-data"
        }
      }]
      restartPolicy = "Always"
    }
  }
}

###############################################################################
# jeffco-sub-monitor — second monitor on this instance.
#
# konlet supervises exactly one container and melanzana has it, so this one is
# started from the startup script instead. Its image repository is created by
# jeffco-sub-monitor/infra and referenced here by path rather than by resource,
# deliberately: that root owns its own images so its builds do not depend on
# this state.
###############################################################################
locals {
  jeffco_image = "${var.region}-docker.pkg.dev/${var.project_id}/jeffco/jeffco-sub-monitor:${var.jeffco_image_tag}"

  jeffco_env = merge(
    {
      SFE_USER_ID         = var.jeffco_sfe_user_id
      SFE_PIN             = var.jeffco_sfe_pin
      DISCORD_WEBHOOK_URL = var.jeffco_discord_webhook_url
      STATE_PATH          = "/data/state.json"
      POLL_INTERVAL_SEC   = tostring(var.jeffco_poll_interval_sec)
    },
    var.jeffco_status_webhook_url != "" ? { STATUS_WEBHOOK_URL = var.jeffco_status_webhook_url } : {},
  )

  jeffco_env_file = join("\n", [for k, v in local.jeffco_env : "${k}=${v}"])
}

###############################################################################
# The e2-micro instance (always-free tier in us-west1/central1/east1).
# Runs the container via the COS container declaration; no inbound ports are
# opened, so the health endpoint (if enabled) is reachable only on-box.
###############################################################################
resource "google_compute_instance" "monitor" {
  name         = "melanzana-monitor"
  machine_type = "e2-micro"
  zone         = var.zone

  boot_disk {
    initialize_params {
      image = data.google_compute_image.cos.self_link
      size  = 30
      type  = "pd-standard"
    }
  }

  network_interface {
    network = "default"
    # Ephemeral external IP — required for outbound internet (Cowlendar/Discord).
    # This is the one real cost (~$3.60/mo for the IPv4 address).
    access_config {}
  }

  metadata = {
    gce-container-declaration = yamlencode(local.container_spec)
    google-logging-enabled    = "true"
    # The container runs as non-root uid 1000 (USER node); make the host volume
    # writable by it before the container starts.
    startup-script = <<-EOT
      #!/bin/bash
      set -euo pipefail

      # melanzana's container runs as non-root uid 1000 (USER node); make the
      # host volume writable by it before konlet starts the container. These two
      # lines predate the second monitor and are deliberately still first.
      mkdir -p /var/lib/melanzana-data
      chown 1000:1000 /var/lib/melanzana-data

      # --- jeffco-sub-monitor ---------------------------------------------
      # GCE re-runs this script on every boot, so everything below is written
      # to be idempotent. `docker run --restart=always` needs no supervisor of
      # its own: Docker restarts always-policy containers when the daemon
      # starts, which is also why a transient pull failure here is survivable —
      # the container object from the previous boot comes back regardless.
      mkdir -p /var/lib/jeffco-data
      chown 1000:1000 /var/lib/jeffco-data

      # /etc is writable on COS but it is a stateless tmpfs, so this file is
      # rewritten every boot by design. base64, not a nested heredoc: a heredoc
      # body inside an interpolated Terraform heredoc arrives carrying its own
      # indentation, and an env-file line reading "  SFE_PIN=x" defines a
      # variable named "  SFE_PIN" — a failure that looks like a bad password.
      mkdir -p /etc/monitors
      chmod 0700 /etc/monitors
      install -m 0600 /dev/null /etc/monitors/jeffco.env
      echo '${base64encode(local.jeffco_env_file)}' | base64 -d > /etc/monitors/jeffco.env

      # docker-credential-gcr writes its config under $DOCKER_CONFIG. The
      # default is $HOME/.docker, and with HOME unset in a startup script that
      # resolves to /.docker — on the read-only root filesystem.
      export DOCKER_CONFIG=/var/lib/docker-config
      mkdir -p "$DOCKER_CONFIG"
      docker-credential-gcr configure-docker --registries="${var.region}-docker.pkg.dev"

      # Retried: at boot this can run before the network is usable, and the
      # first boot after a tag bump has no local copy of the new image to fall
      # back on.
      for attempt in 1 2 3 4 5; do
        docker pull ${local.jeffco_image} && break || sleep 15
      done

      # The loop above always exits 0 — `sleep 15` succeeds where the pull did
      # not — so prove the image is actually on this host before tearing down a
      # container that is currently working. Without this, a registry outage or
      # a typo in jeffco_image_tag turns a redeploy into a silent outage: the
      # old container is gone, the new one cannot start, and nothing can raise
      # an alert because the thing that raises alerts is what failed to start.
      if ! docker image inspect ${local.jeffco_image} >/dev/null 2>&1; then
        echo "jeffco: image unavailable; leaving the running container alone" >&2
        exit 1
      fi

      # --log-opt, because nothing else supplies one: /etc/docker/daemon.json sets
      # only `tag`, so a hand-run container's json-file log grows without bound.
      # konlet gives melanzana 500m x 3 of its own accord; this container has no
      # supervisor to do that for it. 10m x 3 is deliberately generous against the
      # ~9 KB/day observed (the monitor logs events, not polls), and the log lives
      # on the 26 GB stateful partition rather than COS's 2 GB read-only root — so
      # this is hygiene, not headroom.
      docker rm -f jeffco-monitor 2>/dev/null || true
      docker run -d --name jeffco-monitor --restart=always \
        --log-opt max-size=10m --log-opt max-file=3 \
        --env-file /etc/monitors/jeffco.env \
        --volume /var/lib/jeffco-data:/data \
        ${local.jeffco_image}
    EOT
  }

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  # COS expects this label to enable the container runtime agent.
  labels = {
    container-vm = replace(data.google_compute_image.cos.name, ".", "-")
  }

  # Don't recreate the VM just because COS published a newer patch image.
  lifecycle {
    ignore_changes = [boot_disk[0].initialize_params[0].image]
  }
}
