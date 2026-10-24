# Persistent storage for the pod: model weights, saved voices and history.
# Kept in its own stack so destroying the pod never touches it.
resource "runpod_network_volume" "sonorus" {
  name        = var.name
  size        = var.size_gb
  data_center = var.data_center_id
}
