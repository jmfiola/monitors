variable "project_id" {
  type        = string
  description = "GCP project ID to deploy into."
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

variable "image_tag" {
  type        = string
  description = "Tag of the melanzana-monitor image to deploy (e.g. v1.1.0). Build & push to Artifact Registry before applying."
  default     = "latest"
}

# --- Application config (mirrors src/config.ts; only the webhook is required) ---

variable "discord_webhook_url" {
  type        = string
  description = "Discord webhook URL for slot alerts. Passed to the container as an env var (visible in TF state + instance metadata)."
  sensitive   = true
}

variable "status_webhook_url" {
  type        = string
  description = "Optional separate Discord webhook for ops messages (heartbeat/death/recovery). Empty = ops go to the main webhook."
  default     = ""
  sensitive   = true
}

variable "health_port" {
  type        = number
  description = "Optional health endpoint port. 0 = disabled (pure worker, no inbound port opened)."
  default     = 0
}

variable "heartbeat_interval_sec" {
  type        = number
  description = "Heartbeat cadence in seconds (default daily). Raise to quiet the heartbeat."
  default     = 86400
}

variable "stall_alert_sec" {
  type        = number
  description = "No successful poll for this long => death alert + health 503."
  default     = 600
}

variable "window_days" {
  type        = number
  description = "Rolling look-ahead window for bookable slots, in days."
  default     = 60
}

variable "mention_everyone" {
  type        = bool
  description = "Whether real slot alerts ping @everyone."
  default     = false
}

###############################################################################
# jeffco-sub-monitor — the second monitor sharing this instance.
# Its image repository is owned by jeffco-sub-monitor/infra; only the runtime
# configuration lives here, because this root owns the host.
###############################################################################
variable "jeffco_image_tag" {
  description = "Image tag for jeffco-sub-monitor. Use an immutable version tag, not \"latest\"."
  type        = string
}

variable "jeffco_sfe_user_id" {
  description = "SmartFindExpress access id. Sensitive: the PIN closely resembles it."
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

# 60, not the official web client's own 30s refresh, because the account holder
# shares this SFE login with the monitor. Every observed HTTP 400 landed within
# ~3 minutes after an alert went out — i.e. exactly when he opened the site to
# claim the job — and he sees the same conflict from his side, as jobs that only
# appear after a manual refresh. Backing off to 60s halves the share of wall
# clock with a monitor request in flight, which is the only lever here that does
# not require SFE to hand out a second account.
#
# It buys that at the cost of up to 40s of extra notice delay on a listing that
# can be gone in minutes, so this is a trade, not a free win. Do not go lower
# while the login is shared.
variable "jeffco_poll_interval_sec" {
  description = "Seconds between jeffco available-jobs polls."
  type        = number
  default     = 60
}
