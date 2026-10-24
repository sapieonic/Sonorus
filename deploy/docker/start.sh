#!/usr/bin/env bash
# Pod entrypoint: sshd (when RunPod passes a key), the fish-speech API on localhost, then Sonorus.
# Models, voices and history live on the network volume, so a new pod starts warm.
set -euo pipefail

WORKSPACE=${WORKSPACE:-/workspace}
export HF_HOME=${HF_HOME:-$WORKSPACE/hf}
export SONORUS_HOME=${SONORUS_HOME:-$WORKSPACE/sonorus}
export SONORUS_HOST=${SONORUS_HOST:-0.0.0.0}
export SONORUS_PORT=${SONORUS_PORT:-7860}

FISH_ENABLED=${FISH_ENABLED:-1}
FISH_MODEL=${FISH_MODEL:-s2-pro}            # Hugging Face repo fishaudio/<name>
FISH_PORT=${FISH_PORT:-8080}
FISH_COMPILE=${FISH_COMPILE:-0}             # 1 = torch.compile: much faster, but a slow first start
FISH_DIR=$WORKSPACE/fish/checkpoints/$FISH_MODEL

log() { echo "[start $(date +%H:%M:%S)] $*"; }

if [ -n "${PUBLIC_KEY:-}" ]; then
    mkdir -p ~/.ssh && chmod 700 ~/.ssh
    touch ~/.ssh/authorized_keys
    # PUBLIC_KEY can hold several keys, one per line; add each one that's missing.
    while IFS= read -r key; do
        if [ -n "$key" ] && ! grep -qxF "$key" ~/.ssh/authorized_keys; then
            echo "$key" >> ~/.ssh/authorized_keys
        fi
    done <<< "$PUBLIC_KEY"
    chmod 600 ~/.ssh/authorized_keys
    ssh-keygen -A >/dev/null
    /usr/sbin/sshd
    log "sshd started"
fi

mkdir -p "$HF_HOME" "$SONORUS_HOME" "$FISH_DIR"

run_fish() {
    # Retry until the weights are complete: a network blip or a full volume shouldn't disable Fish for good.
    until [ -f "$FISH_DIR/.complete" ]; do
        log "Downloading fishaudio/$FISH_MODEL to $FISH_DIR (one time, ~11 GB for s2-pro)…"
        if /app/.venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('fishaudio/$FISH_MODEL', local_dir='$FISH_DIR')"; then
            touch "$FISH_DIR/.complete"
        else
            log "Download failed (network, or is the volume full?); retrying in 30 s"
            sleep 30
        fi
    done
    local extra=()
    if [ "$FISH_COMPILE" = 1 ]; then extra+=(--compile); fi
    cd /app
    while true; do
        log "Starting Fish Speech ($FISH_MODEL) on 127.0.0.1:$FISH_PORT"
        /app/.venv/bin/python tools/api_server.py \
            --listen "127.0.0.1:$FISH_PORT" \
            --llama-checkpoint-path "$FISH_DIR" \
            "${extra[@]}" || true
        log "Fish Speech stopped; restarting in 10 s"
        sleep 10
    done
}

if [ "$FISH_ENABLED" = 1 ]; then
    # Sonorus starts right away; the UI shows Fish Speech as loading until its server answers.
    run_fish &
    export SONORUS_FISH_URL=http://127.0.0.1:$FISH_PORT
    export SONORUS_FISH_MODEL=${SONORUS_FISH_MODEL:-$FISH_MODEL}
fi

log "Starting Sonorus on $SONORUS_HOST:$SONORUS_PORT"
exec /opt/sonorus/.venv/bin/sonorus serve
