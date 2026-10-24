# The Sonorus GPU pod: web UI + OmniVoice, with fish-speech running beside it on localhost.
# Volume id and data center come from the volume stack (deploy/runpod/volume), applied first.
data "terraform_remote_state" "volume" {
  backend = "local"
  config = {
    path = "${path.module}/../volume/terraform.tfstate"
  }
}

locals {
  # Keyed on the username only: Terraform can't decide a count from a sensitive value.
  private_image = var.registry_username != null
}

# Pull credentials, only for a private image.
resource "runpod_registry" "image" {
  count    = local.private_image ? 1 : 0
  name     = "${var.name}-registry"
  username = var.registry_username
  password = var.registry_password
}

resource "runpod_pod" "sonorus" {
  name     = var.name
  image    = var.image
  registry = local.private_image ? runpod_registry.image[0].id : null

  gpu_type_id = var.gpu_type_id
  gpu_count   = 1
  cloud       = "SECURE"
  # The image is built on CUDA 12.9; older host drivers can't run it.
  min_cuda_version = "12.9"

  data_center_ids = [data.terraform_remote_state.volume.outputs.data_center_id]
  network_volumes = [{
    volume_id = data.terraform_remote_state.volume.outputs.volume_id
    path      = "/workspace"
  }]
  disk = var.container_disk_gb

  # start.sh installs the account's SSH keys (passed as PUBLIC_KEY) and runs sshd.
  start_ssh     = true
  start_jupyter = false
  # 7860 is the public web UI (through RunPod's HTTPS proxy); 22 is SSH. fish-speech stays internal.
  ports = ["7860/http", "22/tcp"]

  env = {
    FISH_ENABLED = var.fish_enabled ? "1" : "0"
    FISH_COMPILE = var.fish_compile ? "1" : "0"
  }
}
