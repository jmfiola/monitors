###############################################################################
# Artifact Registry — one Docker repository per app, named after its key.
#
# Both repositories live here. Previously jeffco's was owned by a second
# Terraform root in the jeffco-sub-monitor repo, which meant the host's
# resources were split across two states for no benefit.
###############################################################################
resource "google_artifact_registry_repository" "app" {
  for_each = var.apps

  location      = var.region
  repository_id = each.key
  format        = "DOCKER"
  description   = "${each.key} container images"
}

###############################################################################
# Dedicated service account for the VM. Least privilege: pull images, write
# logs and metrics. No inbound access, no other cloud permissions.
###############################################################################
resource "google_service_account" "vm" {
  account_id   = "monitors-vm"
  display_name = "monitors host VM"
}

# Granted at the project level rather than per repository: the host pulls every
# app's image, so a per-repository grant would be the same access spelled N times.
resource "google_project_iam_member" "vm" {
  for_each = toset([
    "roles/artifactregistry.reader",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.vm.email}"
}

###############################################################################
# Container-Optimized OS (auto-patching, Docker preinstalled, read-only root).
#
# Note what is NOT set on the instance below: no `gce-container-declaration`
# metadata and no `container-vm` label. Those drive konlet, which supervises
# exactly one container per instance — the reason this host previously ran one
# monitor under konlet and the other from a hand-rolled `docker run`. systemd
# supervises all of them uniformly instead.
###############################################################################
data "google_compute_image" "cos" {
  family  = "cos-stable"
  project = "cos-cloud"
}

locals {
  images = {
    for k, a in var.apps : k => "${var.region}-docker.pkg.dev/${var.project_id}/${k}/${a.image}:${a.image_tag}"
  }

  # Per-app environment. Keys here must match keys in var.apps; the instance has
  # a precondition asserting that, because the failure mode otherwise is an app
  # starting with an empty env file, which looks like a bad password.
  app_env = {
    melanzana = merge(
      {
        DISCORD_WEBHOOK_URL    = var.melanzana_discord_webhook_url
        STATE_PATH             = "/data/state.json"
        WINDOW_DAYS            = tostring(var.melanzana_window_days)
        MENTION_EVERYONE       = tostring(var.melanzana_mention_everyone)
        HEARTBEAT_INTERVAL_SEC = tostring(var.heartbeat_interval_sec)
        STALL_ALERT_SEC        = tostring(var.stall_alert_sec)
      },
      var.melanzana_status_webhook_url != "" ? { STATUS_WEBHOOK_URL = var.melanzana_status_webhook_url } : {},
    )

    jeffco = merge(
      {
        SFE_USER_ID            = var.jeffco_sfe_user_id
        SFE_PIN                = var.jeffco_sfe_pin
        DISCORD_WEBHOOK_URL    = var.jeffco_discord_webhook_url
        STATE_PATH             = "/data/state.json"
        POLL_INTERVAL_SEC      = tostring(var.jeffco_poll_interval_sec)
        HEARTBEAT_INTERVAL_SEC = tostring(var.heartbeat_interval_sec)
        STALL_ALERT_SEC        = tostring(var.stall_alert_sec)
      },
      var.jeffco_status_webhook_url != "" ? { STATUS_WEBHOOK_URL = var.jeffco_status_webhook_url } : {},
    )
  }

  # base64, not a nested heredoc. A heredoc body inside an interpolated Terraform
  # template arrives carrying its own indentation, and an env-file line reading
  # "  SFE_PIN=x" defines a variable named "  SFE_PIN" — a failure that presents
  # as a rejected credential. The same reasoning applies to the unit files, whose
  # `[Section]` headers must start at column zero.
  env_b64 = {
    for k, env in local.app_env : k => base64encode(join("\n", [for ek, ev in env : "${ek}=${ev}"]))
  }

  units_b64 = {
    for k, a in var.apps : k => base64encode(templatefile("${path.module}/templates/monitor.service.tftpl", {
      name   = k
      image  = local.images[k]
      memory = a.memory
    }))
  }
}

###############################################################################
# The e2-micro (always-free in us-west1/central1/east1). No inbound ports are
# opened; every monitor is an outbound poller.
###############################################################################
resource "google_compute_instance" "host" {
  name         = "monitors"
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
    # Ephemeral external IP — required for outbound internet. This is the only
    # real line item (~$0.005/hr); the instance and disk are within the free tier.
    access_config {}
  }

  metadata = {
    google-logging-enabled = "true"
    startup-script = templatefile("${path.module}/templates/startup.sh.tftpl", {
      region    = var.region
      images    = local.images
      env_b64   = local.env_b64
      units_b64 = local.units_b64
    })
  }

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  # Don't recreate the VM just because COS published a newer patch image. The
  # running instance auto-updates itself; this only stops Terraform proposing a
  # replacement, which would destroy the boot disk and every app's state.json.
  lifecycle {
    ignore_changes = [boot_disk[0].initialize_params[0].image]

    precondition {
      condition     = length(setsubtract(keys(var.apps), keys(local.app_env))) == 0
      error_message = "Every app in var.apps needs an entry in local.app_env. Missing: ${join(", ", setsubtract(keys(var.apps), keys(local.app_env)))}"
    }
  }

  depends_on = [google_project_iam_member.vm]
}
