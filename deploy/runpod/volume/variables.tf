variable "data_center_id" {
  description = "Data center for the volume, e.g. \"CA-MTL-1\". It must offer network volumes, and the pod runs there too, so it needs Secure Cloud stock of your gpu_type_id. Changing it replaces the volume and its contents; apply this stack before the pod stack."
  type        = string
}

variable "name" {
  description = "Volume name in the RunPod console."
  type        = string
  default     = "sonorus-data"
}

variable "size_gb" {
  description = "Size in GB. The models take about 16 GB; the rest is for voices and history. Can grow later, never shrink."
  type        = number
  default     = 50

  validation {
    condition     = var.size_gb >= 20 && var.size_gb <= 4096
    error_message = "size_gb must be between 20 and 4096 (the models alone need about 16 GB)."
  }
}
