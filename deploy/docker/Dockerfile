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

# Dependencies first so code changes don't reinstall torch. The cu129 build matches the base image's CUDA 12.9.
COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    printf 'torch==2.11.0\ntorchaudio==2.11.0\n' > /opt/sonorus/torch.txt \
    && uv venv .venv --python /usr/bin/python3 \
    && uv pip install --python .venv/bin/python --index-url https://download.pytorch.org/whl/cu129 -r torch.txt \
    && uv pip install --python .venv/bin/python -c torch.txt -r pyproject.toml

COPY hogwarts_voice ./hogwarts_voice
RUN uv pip install --python .venv/bin/python --no-deps . && rm -rf hogwarts_voice

COPY deploy/docker/start.sh /opt/sonorus/start.sh
RUN chmod +x /opt/sonorus/start.sh

ENV PATH=/opt/sonorus/.venv/bin:$PATH
EXPOSE 7860
ENTRYPOINT ["/opt/sonorus/start.sh"]
