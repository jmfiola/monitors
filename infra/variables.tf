variable "project_id" {
  type        = string
  description = "GCP project ID. Defaulted here rather than in terraform.tfvars so git records which project is targeted; it is not a secret."
  default     = "cobs-cloud"
}

variable "region" {
  type        = string
  description = "Free-tier region for the e2-micro (us-west1, us-central1, or us-east1)."
  default     = "us-west1"

  validation {
    condition     = contains(["us-west1", "us-central1", "us-east1"], var.region)
    error_message = "e2-micro is only always-free in us-west1, us-central1, or us-east1."
  }
}

variable "zone" {
  type        = string
  description = "Zone within the region for the instance."
  default     = "us-west1-b"
}

###############################################################################
# The app list. This is the file you edit to deploy, and it is committed —
# see apps.auto.tfvars.
#
# `memory` is a leak backstop, not a tuning knob: it should sit well above
# normal operation. The instance has no swap, so without a cap the kernel picks
# the OOM victim, and it may pick a healthy monitor over the leaking one.
#
# Paths are derived from the key, so there is one fewer thing to get wrong per
# app: the container and systemd unit are <key>-monitor, the data volume is
# /var/lib/<key>-data, the env file is /etc/monitors/<key>.env, and the Artifact
# Registry repository is <key>.
#
# `image` is explicit rather than derived because the published images do not
# follow one rule — melanzana's is `melanzana-monitor`, jeffco's is
# `jeffco-sub-monitor`. Inventing a convention here would mean renaming a
# published image to satisfy a pattern.
###############################################################################
variable "apps" {
  description = "Monitors to run on the host, keyed by name."
  type = map(object({
    image     = string
    image_tag = string
    memory    = string
  }))

  validation {
    condition     = alltrue([for a in var.apps : can(regex("^[0-9]+[kmg]$", a.memory))])
    error_message = "memory must be a Docker size string such as 128m or 1g."
  }

  validation {
    condition     = alltrue([for a in var.apps : a.image_tag != "latest"])
    error_message = "Use an immutable version tag, not \"latest\" — otherwise git does not record what is deployed."
  }
}

###############################################################################
# melanzana-monitor — Cowlendar appointment slots.
###############################################################################
variable "melanzana_discord_webhook_url" {
  type        = string
  description = "Discord webhook for slot alerts. A credential: anyone holding it can post."
  sensitive   = true
}

variable "melanzana_status_webhook_url" {
  type        = string
  description = "Optional separate Discord webhook for melanzana ops messages. Empty = use the alert channel."
  default     = ""
  sensitive   = true
}

variable "melanzana_window_days" {
  type        = number
  description = "Rolling look-ahead window for bookable slots, in days."
  default     = 60
}

variable "melanzana_mention_everyone" {
  type        = bool
  description = "Whether real slot alerts ping @everyone."
  default     = false
}

# 07:00 America/Denver — early enough to read over coffee and still ahead of most
# of the day's job postings, so a missing heartbeat can be acted on before the
# window that matters. Empty falls back to HEARTBEAT_INTERVAL_SEC, which anchors
# the heartbeat to process start and therefore reports at whatever time the last
# deploy happened.
variable "melanzana_heartbeat_at" {
  type        = string
  description = "HH:MM America/Denver time for melanzana's daily heartbeat. Empty = use HEARTBEAT_INTERVAL_SEC."
  default     = "07:00"

  validation {
    condition     = var.melanzana_heartbeat_at == "" || can(regex("^([01][0-9]|2[0-3]):[0-5][0-9]$", var.melanzana_heartbeat_at))
    error_message = "melanzana_heartbeat_at must be empty or an HH:MM 24-hour time."
  }
}

###############################################################################
# jeffco-sub-monitor — Jeffco substitute teaching jobs.
###############################################################################
variable "jeffco_sfe_user_id" {
  description = "SmartFindExpress access id. Sensitive: the PIN closely resembles it, so leaking one leaks most of the other."
  type        = string
  sensitive   = true
}

variable "jeffco_sfe_pin" {
  description = "SmartFindExpress PIN."
  type        = string
  sensitive   = true
}

variable "jeffco_discord_webhook_url" {
  description = "Discord webhook for job alerts. A credential — anyone holding it can post."
  type        = string
  sensitive   = true
}

variable "jeffco_status_webhook_url" {
  description = "Optional separate Discord webhook for jeffco ops messages. Empty = use the alert channel."
  type        = string
  sensitive   = true
  default     = ""
}

# 60s, deliberately slower than the official SFE web client's own 30s refresh,
# because the monitor shares one login with the substitute it watches for. SFE
# answers HTTP 400 while a second session is active on the account, and the
# evidence that this is what the 400s are is diurnal: across the monitor's first
# ~59 hours, all 34 of them landed between 06:00 and midnight, with none in three
# nights of overnight polling. He sees the same conflict from his side, as jobs
# that only appear after a manual refresh.
#
# It costs up to 40s of extra notice on a listing that can be gone in minutes, so
# this is a trade, not a free win. Do not go lower while the login is shared.
variable "jeffco_poll_interval_sec" {
  description = "Seconds between jeffco available-jobs polls."
  type        = number
  default     = 60
}

# 07:00 America/Denver — same reasoning as melanzana's: early enough to read over
# coffee and still ahead of most of the day's job postings, so a missing
# heartbeat can be acted on before the window that matters. Empty falls back to
# HEARTBEAT_INTERVAL_SEC, which anchors the heartbeat to process start and
# therefore reports at whatever time the last deploy happened.
variable "jeffco_heartbeat_at" {
  type        = string
  description = "HH:MM America/Denver time for jeffco's daily heartbeat. Empty = use HEARTBEAT_INTERVAL_SEC."
  default     = "07:00"

  validation {
    condition     = var.jeffco_heartbeat_at == "" || can(regex("^([01][0-9]|2[0-3]):[0-5][0-9]$", var.jeffco_heartbeat_at))
    error_message = "jeffco_heartbeat_at must be empty or an HH:MM 24-hour time."
  }
}

###############################################################################
# fashionjobs-monitor — France-wide FashionJobs Stage listings.
###############################################################################
variable "fashionjobs_discord_webhook_url" {
  type        = string
  description = "Discord webhook for FashionJobs internship alerts. A credential: anyone holding it can post."
  sensitive   = true

  validation {
    condition     = can(regex("^https://[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+(:[0-9]{1,5})?(/[^[:space:]]*)?$", var.fashionjobs_discord_webhook_url))
    error_message = "fashionjobs_discord_webhook_url must be an https:// URL."
  }
}

variable "fashionjobs_status_webhook_url" {
  type        = string
  description = "Optional separate Discord webhook for FashionJobs ops messages. Empty = use the alert channel."
  default     = ""
  sensitive   = true

  validation {
    condition     = var.fashionjobs_status_webhook_url == "" || can(regex("^https://[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+(:[0-9]{1,5})?(/[^[:space:]]*)?$", var.fashionjobs_status_webhook_url))
    error_message = "fashionjobs_status_webhook_url must be empty or an https:// URL."
  }
}

variable "fashionjobs_poll_interval_sec" {
  type        = number
  description = "Seconds between FashionJobs Stage listing polls."
  default     = 600

  validation {
    condition     = var.fashionjobs_poll_interval_sec > 0
    error_message = "fashionjobs_poll_interval_sec must be positive."
  }
}

variable "fashionjobs_heartbeat_at" {
  type        = string
  description = "HH:MM America/Denver time for FashionJobs' daily heartbeat. Empty = use HEARTBEAT_INTERVAL_SEC."
  default     = "07:00"

  validation {
    condition     = var.fashionjobs_heartbeat_at == "" || can(regex("^([01][0-9]|2[0-3]):[0-5][0-9]$", var.fashionjobs_heartbeat_at))
    error_message = "fashionjobs_heartbeat_at must be empty or an HH:MM 24-hour time."
  }
}

###############################################################################
# Shared ops config. Applied to every app; the library reads the same names.
###############################################################################
variable "heartbeat_interval_sec" {
  type        = number
  description = "Heartbeat cadence in seconds (default daily). Raise to quiet it."
  default     = 86400
}

variable "stall_alert_sec" {
  type        = number
  description = "No successful poll for this long => death alert."
  default     = 600
}
