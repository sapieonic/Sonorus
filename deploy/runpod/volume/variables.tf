variable "data_center_id" {
  description = "Data center for the volume, e.g. \"EU-RO-1\". The pod has to run there too, so pick one with 48 GB GPUs in Secure Cloud. Changing it replaces the volume and its contents."
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
