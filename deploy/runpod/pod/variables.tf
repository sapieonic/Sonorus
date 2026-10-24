variable "image" {
  description = "Image pushed by deploy/build-push.sh, e.g. \"docker.io/you/sonorus:0.2.0\". Use a new tag per build."
  type        = string
}

variable "name" {
  description = "Pod name in the RunPod console."
  type        = string
  default     = "sonorus"
}

variable "gpu_type_id" {
  description = "RunPod GPU type. Both engines on one card need about 30 GB, so use a 48 GB card: \"NVIDIA L40S\", \"NVIDIA RTX A6000\", \"NVIDIA A40\" or \"NVIDIA RTX 6000 Ada Generation\". With fish_enabled = false a 24 GB card is plenty. Changing it replaces the pod."
  type        = string
  default     = "NVIDIA L40S"
}

variable "container_disk_gb" {
  description = "Scratch disk inside the container. Models live on the network volume, so this stays small."
  type        = number
  default     = 30
}

variable "fish_enabled" {
  description = "Run Fish Speech (S2-Pro) next to OmniVoice. false = OmniVoice only."
  type        = bool
  default     = true
}

variable "fish_compile" {
  description = "Run fish-speech with torch.compile: much faster generation, but several extra minutes on every start."
  type        = bool
  default     = false
}

variable "registry_username" {
  description = "Registry user, only for a private image. Leave unset for a public one."
  type        = string
  default     = null
}

variable "registry_password" {
  description = "Registry password or access token, only for a private image. Stored in the local Terraform state."
  type        = string
  default     = null
  sensitive   = true
}
