# Deploying Sonorus on RunPod

This guide puts Sonorus on a RunPod GPU pod with both engines, OmniVoice and Fish Speech (S2-Pro), and lets you
switch between them from the web UI. Everything runs from your own machine: you build and push the image with
Docker, then create the pod with Terraform. Nothing is deployed by CI.

## What you end up with

```
                    https://<pod-id>-7860.proxy.runpod.net   (public, no login yet)
                                       │
┌─ RunPod GPU pod (48 GB GPU) ─────────┼──────────────────────────────────────────┐
│                                      ▼                                          │
│   Sonorus web UI + API  :7860   ── OmniVoice (in-process, CUDA)                 │
│          │                                                                      │
│          └── HTTP ──► fish-speech API server  127.0.0.1:8080 (not exposed)      │
│                                                                                 │
│   /workspace  ◄── RunPod network volume (survives pod rebuilds)                 │
│     ├── hf/                 OmniVoice + Whisper weights  (~5 GB)                │
│     ├── fish/checkpoints/   Fish Speech S2-Pro weights   (~11 GB)               │
│     └── sonorus/            your saved voices (vault) and history (Pensieve)    │
└─────────────────────────────────────────────────────────────────────────────────┘
```

- **One image, one pod.** The image starts from the official `fishaudio/fish-speech` server image and adds
  Sonorus in a separate Python environment, so the two models' dependencies never clash.
- **One GPU.** S2-Pro needs about 24 GB and OmniVoice a few GB more, so the pod uses a 48 GB card
  (L40S, RTX A6000, A40 or RTX 6000 Ada).
- **Two Terraform stacks.** `deploy/runpod/volume` creates the storage and `deploy/runpod/pod` the GPU pod.
  You can destroy the pod whenever you stop experimenting without losing models, voices or history.

| File | Purpose |
|---|---|
| `deploy/docker/Dockerfile` | The pod image |
| `deploy/docker/start.sh` | Pod entrypoint: starts sshd, fish-speech (downloads S2-Pro on first boot), then Sonorus |
| `deploy/build-push.sh` | Builds the image for `linux/amd64` and pushes it |
| `deploy/runpod/volume/` | Terraform: the network volume |
| `deploy/runpod/pod/` | Terraform: the GPU pod |

The Terraform uses RunPod's official provider, `runpod/runpod`, pinned to `2.0.0-rc.3`. 2.0 is still a release
candidate (the last stable release, 1.0.9, has a different schema), so it is pinned exactly; move to `~> 2.0` once
2.0.0 is final.

## 1. Before you start

You need:

1. **A RunPod account with credit.** Pods bill per second while they run; the network volume bills per GB per month
   even while no pod is running. Check current prices on RunPod's pricing page.
2. **A RunPod API key.** Console → Settings → API Keys → create one with read/write access.
3. **Your SSH public key in RunPod** (optional, for `ssh` into the pod). Console → Settings → SSH Public Keys.
   RunPod passes it to the pod and `start.sh` installs it.
4. **Docker Desktop, running**, with about 40 GB of free disk for the build. The image is large: the fish-speech
   base is about 6 GB compressed and Sonorus's PyTorch adds a few more.
5. **An image registry account**, e.g. Docker Hub (`docker.io/<you>/sonorus`) or GitHub (`ghcr.io/<you>/sonorus`).
6. **Terraform 1.6 or newer** (`terraform version`).

Set the API key in the shell you'll run Terraform from:

```bash
export RUNPOD_API_KEY=rpa_xxxxxxxxxxxxxxxx   # never commit this
```

## 2. Build and push the image

From the repo root:

```bash
docker login                                   # or: docker login ghcr.io
deploy/build-push.sh docker.io/<you>/sonorus:0.2.0
```

The script builds for `linux/amd64`, the architecture of RunPod's GPU machines. On an Apple Silicon Mac that means a
cross-build under emulation, so the first build is slow. Later builds reuse the cached layers, and changing only
Sonorus code rebuilds just the last few layers.

If the build stops with `Failed to fetch: https://pypi.org/...` and `operation timed out`, the connection to PyPI was
too slow, not the Dockerfile. Run the script again: downloaded packages are cached, so it picks up where it stopped.
The Dockerfile already allows 300 s per request and 10 retries; on a very slow link raise them, e.g. add
`--build-arg UV_HTTP_TIMEOUT=600 --build-arg UV_CONCURRENT_DOWNLOADS=2` to the `docker buildx build` command in
`deploy/build-push.sh`. If it never gets through, check whether a VPN or proxy is blocking `pypi.org` and
`files.pythonhosted.org`.

**Make the image pullable by RunPod.** The simplest way is to make the repository public; the image holds no secrets,
only code and libraries. If you keep it private, set `registry_username` and `registry_password` in the pod stack
(step 5). For Docker Hub use an access token as the password; for GHCR use a token with `read:packages`.

Use a new tag for every build (`0.2.0`, `0.2.1`, …) rather than reusing one. Terraform only sees a change when the
`image` value changes, so pushing over an existing tag would never reach the pod.

## 3. Pick a data center

A network volume lives in one data center, and the pod must run in the same one. So the data center needs both:

1. **Network volumes.** Not every data center offers them (`EU-SE-1` didn't when this was written).
2. **Stock of the GPU you want, in Secure Cloud.** Stock changes by the minute, and a data center can have none of a
   given card at all.

Pick the GPU first. Both engines together need about 30 GB of GPU memory; OmniVoice alone fits in 16–24 GB.

| `gpu_type_id` | Card | Memory |
|---|---|---|
| `NVIDIA A40` | A40 (cheapest 48 GB option, slower) | 48 GB |
| `NVIDIA RTX A6000` | RTX A6000 | 48 GB |
| `NVIDIA L40S` | L40S (the default) | 48 GB |
| `NVIDIA RTX 6000 Ada Generation` | RTX 6000 Ada | 48 GB |
| `NVIDIA A100 80GB PCIe` / `NVIDIA A100-SXM4-80GB` | A100 | 80 GB |
| `NVIDIA H100 PCIe` / `NVIDIA H100 80GB HBM3` | H100 PCIe / H100 SXM | 80 GB |

Then find where it's in stock. In the console: Pods → Deploy, filter to Secure Cloud and that card, and check the
regions offered; Storage → New Network Volume shows which data centers take volumes. Or ask the API (read-only):

```bash
GPU="NVIDIA H100 80GB HBM3"
curl -s https://api.runpod.io/graphql -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -d '{"query":"query { dataCenters { id gpuAvailability { gpuTypeId stockStatus } } }"}' \
  | GPU="$GPU" python3 -c 'import json,os,sys; [print(dc["id"], g["stockStatus"]) for dc in json.load(sys.stdin)["data"]["dataCenters"] for g in dc["gpuAvailability"] if g["gpuTypeId"] == os.environ["GPU"] and g["stockStatus"]]'
```

Prefer `High` or `Medium` stock; with `Low` you may have to retry the pod a few times. Note the data center id (for
example `CA-MTL-1`) for the volume, and the `gpu_type_id` for the pod.

## 4. Create the network volume (once)

```bash
cd deploy/runpod/volume
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: data_center_id, and optionally the size (default 50 GB)
terraform init
terraform apply
```

Both stacks keep their state in a local `terraform.tfstate` file next to the code; it is git-ignored. Keep it:
without it Terraform loses track of what it created. This stack holds your data. Leave it in place between experiments, and only destroy it when you want to delete
the models, voices and history for good.

## 5. Create the pod

```bash
cd ../pod
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: image = "docker.io/<you>/sonorus:0.2.0"
terraform init
terraform apply
terraform output
```

The pod stack reads the volume id and data center from the volume stack's **state**, not from its `terraform.tfvars`,
so you don't copy them across. It also means a changed `data_center_id` only reaches the pod after you apply the
volume stack. Always apply the volume stack first.
It also asks for a host whose driver supports CUDA 12.6 or newer (`min_cuda_version`), because both engines use
CUDA 12.6 builds of PyTorch.
The outputs give you:

- `url`: the public web UI, `https://<pod-id>-7860.proxy.runpod.net`
- `pod_id`, `gpu`, and `cost_per_hour_usd`
- `ssh_command`: only once the pod is running and RunPod has assigned the SSH port. If it's empty, wait a minute,
  then run `terraform apply -refresh-only` and `terraform output ssh_command`

Variables you can set in `deploy/runpod/pod/terraform.tfvars`:

| Variable | Default | What it does |
|---|---|---|
| `image` | (required) | The image you pushed in step 2 |
| `name` | `sonorus` | Pod name in the RunPod console |
| `gpu_type_id` | `NVIDIA L40S` | RunPod GPU type; see the table in step 3 (e.g. `NVIDIA A40`, `NVIDIA H100 80GB HBM3` for H100 SXM). It must be in stock in the volume's data center. Changing it replaces the pod |
| `container_disk_gb` | `30` | Scratch disk inside the container (models live on the volume, not here) |
| `fish_enabled` | `true` | `false` runs OmniVoice only, so a 16–24 GB GPU is enough |
| `fish_compile` | `false` | `true` runs fish-speech with `torch.compile`: much faster speech, slow first start |
| `registry_username` / `registry_password` | unset | Only for a private image |

## 6. First boot

The first boot downloads everything onto the empty volume:

1. RunPod pulls the image (a few minutes, depending on the host).
2. `start.sh` starts sshd, then starts the S2-Pro download (~11 GB) in the background, then starts Sonorus.
3. Sonorus downloads OmniVoice (~3.3 GB) and loads it. The UI is usable with OmniVoice as soon as this finishes.
4. When S2-Pro is downloaded, fish-speech loads it and starts listening. Until then the UI shows Fish Speech
   as "waiting… (it may still be loading)" and casting with it is disabled.

Watch progress in the RunPod console: Pods → your pod → Logs. Lines starting with `[start` come from `start.sh`.

Later pods start warm: the weights are already on the volume, so you only wait for the image pull and model loading.

## 7. Use it

Open the `url` output in a browser.

- The switch at the top right (**OmniVoice | Fish Speech**) picks the engine. The status pill next to it shows
  the selected engine's state.
- With Fish Speech, voice design, language, speed, exact length and "Spell out numbers" are off because S2-Pro
  doesn't support them. Steer delivery with inline tags instead: `[whisper]`, `[excited]`, `[pause]`, `[laughing]`,
  `[angry]`, `[emphasis]` (buttons above the text box), or any short natural-language instruction in brackets.
- Long text works with both engines. For Fish Speech, Sonorus splits it at sentence ends into pieces of about 300
  characters ("Piece length" in the Restricted Section) and joins the audio. A tag applies to the piece it's in.
- Saved voices work with both engines. Fish Speech clones best from a 10–30 second clip; OmniVoice prefers 3–10.
  If you don't type the transcript, Whisper writes one. For Fish Speech it transcribes the whole clip the first time
  you use the voice, so that first spell takes a little longer.
- RunPod's proxy closes any request that runs past about 100 seconds. The UI and CLI don't hit this: they start
  generation as a job and poll it. If you call the API yourself, use `POST /api/speak?wait=false` and poll
  `GET /api/jobs/{id}`.
- The Pensieve marks Fish Speech results with a **FISH** badge, and "reuse" switches back to that engine.

The CLI on your Mac can drive the pod too:

```bash
export SONORUS_URL=https://<pod-id>-7860.proxy.runpod.net
sonorus speak "Mischief managed." -o out.wav --play                                  # OmniVoice
sonorus speak "[whisper] Mischief managed." -e fish -v minerva -o out.wav --play     # Fish Speech
sonorus clone minerva clip.wav -t "What the clip says."                              # saved on the pod's volume
```

## 8. Stop paying when you're done

```bash
cd deploy/runpod/pod
terraform destroy
```

This deletes the pod and stops the GPU billing. The volume and everything on it stay, at the storage rate.
Run `terraform apply` again when you want it back.

To delete everything, destroy the pod stack first, then the volume stack:

```bash
cd deploy/runpod/volume && terraform destroy
```

## 9. Ship a new version

```bash
deploy/build-push.sh docker.io/<you>/sonorus:0.2.1
# set image = "docker.io/<you>/sonorus:0.2.1" in deploy/runpod/pod/terraform.tfvars
cd deploy/runpod/pod && terraform apply
```

Terraform updates the pod in place and it restarts on the new image. If it ever keeps running the old one, force a
fresh pod with `terraform apply -replace=runpod_pod.sonorus`. Either way, voices, history and model weights carry
over on the volume.

## Security

The UI and API are public: anyone who has the proxy URL can generate speech, see your history and use your saved
voices, and it all bills to your GPU. The URL contains the random pod id and isn't listed anywhere, but treat it as
a secret. Destroy the pod when you're not using it, and add a login layer before sharing it.

The fish-speech server listens only on `127.0.0.1` inside the pod and isn't exposed.

## Troubleshooting

| Symptom | What to do |
|---|---|
| Pod creation fails with "There are no longer any instances available with the requested specifications" | No free card of that `gpu_type_id` (Secure Cloud, CUDA 12.6+) in the volume's data center right now. First check the pod plan's `data_center_ids`: if it shows the old data center, you changed the volume's `tfvars` without applying the volume stack. Otherwise retry in a few minutes, pick another `gpu_type_id`, or move: set a new `data_center_id` (step 3), apply the volume stack (this replaces the volume and its contents), then apply the pod |
| Pod keeps restarting, logs show an image pull error | The image is private and the registry credentials are missing or wrong, or the tag doesn't exist. Check `image`, `registry_username`, `registry_password` |
| Logs show a CUDA or driver error | Shouldn't happen with `min_cuda_version = "12.6"`, but if it does, recreate the pod (`terraform apply -replace=runpod_pod.sonorus`) to land on another host |
| Fish Speech stays on "waiting…" | Check the logs. `start.sh` retries a failed S2-Pro download every 30 s (a full volume is a common cause: grow `size_gb` and apply the volume stack) and restarts fish-speech 10 s after it exits. The CLI gives up waiting after 20 minutes (`SONORUS_WAIT` in seconds changes that) |
| The UI loads but says "No wand found" | Sonorus itself isn't answering yet (still loading) or crashed; check the logs |
| Out-of-memory errors | Use a bigger card, or set `fish_enabled = false` to free the GPU for OmniVoice |
| `ssh_command` output is empty | RunPod hadn't assigned the port yet: `terraform apply -refresh-only`, then `terraform output ssh_command`, or use Connect in the console |
| `sonorus` says it can't reach the server | With `SONORUS_URL` set, the CLI never falls back to loading the model on your Mac; check the URL and that the pod is running |
