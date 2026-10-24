#!/usr/bin/env bash
# Build the pod image for linux/amd64 (RunPod's GPUs) and push it.
#   deploy/build-push.sh docker.io/<you>/sonorus:0.2.0
# Log in first (docker login / docker login ghcr.io). On Apple Silicon this cross-builds, so the first build is slow.
set -euo pipefail

IMAGE=${1:?usage: deploy/build-push.sh <registry/name:tag>}
cd "$(dirname "$0")/.."

docker buildx build \
    --platform linux/amd64 \
    -f deploy/docker/Dockerfile \
    -t "$IMAGE" \
    --push \
    .

echo "Pushed $IMAGE. Set image = \"$IMAGE\" in deploy/runpod/pod/terraform.tfvars and run terraform apply there."
