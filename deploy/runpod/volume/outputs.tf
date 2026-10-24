output "volume_id" {
  description = "Network volume id; the pod stack reads it from this stack's state."
  value       = runpod_network_volume.sonorus.id
}

output "data_center_id" {
  description = "Data center the volume lives in; the pod is placed there."
  value       = runpod_network_volume.sonorus.data_center
}
