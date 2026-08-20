output "instance_name" {
  value       = google_compute_instance.monitor.name
  description = "Name of the monitor VM."
}

output "zone" {
  value       = google_compute_instance.monitor.zone
  description = "Zone the VM runs in."
}

output "external_ip" {
  value       = google_compute_instance.monitor.network_interface[0].access_config[0].nat_ip
  description = "Ephemeral external IP (outbound only; no inbound ports are opened)."
}

output "image" {
  value       = local.image
  description = "Full Artifact Registry image reference the VM pulls."
}

output "logs_command" {
  value       = "gcloud compute instances get-serial-port-output ${google_compute_instance.monitor.name} --zone ${google_compute_instance.monitor.zone}"
  description = "Quick serial-console peek (includes container stdout)."
}

output "ssh_logs_command" {
  value       = "gcloud compute ssh ${google_compute_instance.monitor.name} --zone ${google_compute_instance.monitor.zone} --command 'docker logs $(docker ps -q --filter name=melanzana-monitor) --tail 50 -f'"
  description = "Tail the container logs over SSH."
}
