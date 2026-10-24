# Sonorus pod image: the official fish-speech server image, plus Sonorus (OmniVoice) in its own venv.
# fish-speech lives in /app with its own pinned torch; Sonorus gets a separate venv so the two never clash.
# Build from the repo root:  deploy/build-push.sh <image>
ARG FISH_IMAGE=fishaudio/fish-speech:server-cuda-v3.0.0-beta
FROM ${FISH_IMAGE}

USER root
ENV DEBIAN_FRONTEND=noninteractive

# sshd so `ssh` and `scp` work through RunPod's TCP port mapping.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd \
    && sed -i 's/^#\?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config

WORKDIR /opt/sonorus
ENV UV_LINK_MODE=copy

# uv gives up on PyPI after 30 s and 3 retries by default, which a slow link (or amd64 emulation on Apple Silicon)
# easily hits. Build args, so they stay out of the final image; override with --build-arg if needed.
ARG UV_HTTP_TIMEOUT=300
ARG UV_HTTP_RETRIES=10
ARG UV_CONCURRENT_DOWNLOADS=4

# Dependencies first so code changes don't reinstall torch. cu126 like fish-speech's own torch in the base image,
# so the pod only needs a host driver for CUDA 12.6+. torch gets its own layer: it's the big download.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv .venv --python /usr/bin/python3 \
    && uv pip install --python .venv/bin/python --index-url https://download.pytorch.org/whl/cu126 torch==2.11.0 torchaudio==2.11.0

# The rest of the model stack, pinned to the versions Sonorus was tested with.
COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    printf 'torch==2.11.0\ntorchaudio==2.11.0\nomnivoice==0.2.1\ntransformers==5.20.2\n' > constraints.txt \
    && uv pip install --python .venv/bin/python -c constraints.txt -r pyproject.toml

COPY hogwarts_voice ./hogwarts_voice
RUN uv pip install --python .venv/bin/python --no-deps . && rm -rf hogwarts_voice

COPY deploy/docker/start.sh /opt/sonorus/start.sh
RUN chmod +x /opt/sonorus/start.sh

ENV PATH=/opt/sonorus/.venv/bin:$PATH
# The base image's healthcheck probes fish-speech on :8080; Sonorus on :7860 is what has to be up.
HEALTHCHECK --interval=30s --timeout=10s --start-period=600s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7860/api/status >/dev/null || exit 1
EXPOSE 7860
ENTRYPOINT ["/opt/sonorus/start.sh"]
