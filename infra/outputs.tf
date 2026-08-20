output "project_id" {
  description = "Project the host runs in."
  value       = var.project_id
}

output "instance_name" {
  value = google_compute_instance.host.name
}

output "zone" {
  value = var.zone
}

output "external_ip" {
  description = "Ephemeral outbound IP. Nothing listens on it; no inbound ports are opened."
  value       = google_compute_instance.host.network_interface[0].access_config[0].nat_ip
}

output "images" {
  description = "Exactly what is deployed, per app. Also recorded in apps.auto.tfvars, which is committed."
  value       = local.images
}

output "units" {
  description = "systemd units on the host, for `systemctl status`."
  value       = [for k in keys(var.apps) : "${k}-monitor.service"]
}
