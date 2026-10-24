terraform {
  required_version = ">= 1.6"

  required_providers {
    runpod = {
      source = "runpod/runpod"
      # 2.0 is still a release candidate; Terraform only installs one when pinned exactly.
      version = "2.0.0-rc.3"
    }
  }
}

# Reads the API key from RUNPOD_API_KEY.
provider "runpod" {}
