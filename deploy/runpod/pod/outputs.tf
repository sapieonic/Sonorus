locals {
  # Port mappings are only known once the pod is RUNNING; `terraform refresh` fills them in later.
  ssh_port = try([for p in runpod_pod.sonorus.runtime.ports : p if p.private_port == 22][0], null)
}

output "url" {
  description = "Public web UI. No login: treat it as a secret."
  value       = "https://${runpod_pod.sonorus.id}-7860.proxy.runpod.net"
}

output "pod_id" {
  value = runpod_pod.sonorus.id
}

output "gpu" {
  value = runpod_pod.sonorus.gpu_type_id
}

output "cost_per_hour_usd" {
  description = "What the pod costs per hour while it runs."
  value       = runpod_pod.sonorus.cost
}

output "ssh_command" {
  description = "Direct SSH into the pod. Null until the pod is running; run `terraform refresh` and check again."
  value       = local.ssh_port == null ? null : "ssh root@${local.ssh_port.ip} -p ${local.ssh_port.public_port}"
}
