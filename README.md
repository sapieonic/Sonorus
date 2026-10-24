# Sonorus 🪄

A Hogwarts-themed voice cloning and text-to-speech service built on
[OmniVoice](https://github.com/k2-fsa/OmniVoice) (600+ languages, zero-shot cloning, voice design) and,
optionally, [Fish Speech](https://github.com/fishaudio/fish-speech) (expressive speech steered by inline tags).
You get a web UI with every option of both engines, plus a `sonorus` CLI for scripting.

| Spell | What it does |
|---|---|
| 🧪 **Polyjuice Potion** | Clone a voice from a short clip (upload, drag-drop or record in the browser) |
| ✨ **Transfiguration** | Design a voice from attributes: gender, age, pitch, whisper, English accent, Chinese dialect |
| 🎩 **Sorting Hat** | Let the model pick a voice |
| 🏦 **Gringotts Vault** | Saved voices, reusable from the UI and CLI |
| 🌀 **Pensieve** | History of everything you generated: replay, download, reuse settings, copy as CLI command |
| 🔒 **Restricted Section** | All generation knobs: steps, guidance, temperatures, shifts, chunking, padding, fades… |

## Setup

```bash
python3 -m venv .venv
# PyTorch first. Pick the wheel for your GPU (cu126 still covers older Pascal cards; cu128 for newer ones):
.venv/bin/pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126
# Apple Silicon Mac: plain PyPI wheels include Metal (MPS) support. Intel Macs are not supported.
# .venv/bin/pip install torch torchaudio
.venv/bin/pip install -e .           # add [tn] to enable "spell out numbers"
source .venv/bin/activate
```

The model (~3.3 GB) downloads from Hugging Face on first run into `~/.cache/huggingface`.

## Two engines

| | OmniVoice | Fish Speech (S2-Pro) |
|---|---|---|
| Runs | in-process, on CUDA / Apple MPS / CPU | as a separate fish-speech API server; needs an NVIDIA GPU with 24 GB+ |
| Cloning | 3–10 s clip | 10–30 s clip works best |
| Voice design (gender, age, accent…) | yes | no |
| Speed / exact length / language / spell out numbers | yes | no (it detects the language and reads numbers itself) |
| Long text | split by OmniVoice | split by Sonorus at sentence ends ("Piece length"), then joined |
| Expressive control | sound tags like `[laughter]`, `[sigh]` | free-form tags like `[whisper]`, `[excited]`, `[pause]` |

OmniVoice is always on. To add Fish Speech, start a fish-speech API server and point Sonorus at it with
`SONORUS_FISH_URL` (the RunPod image does this for you; see [runpod-deployment-guide.md](runpod-deployment-guide.md)).
The switch in the top-right of the UI then picks the engine, and the CLI takes `--engine fish`.
Saved voices work with both: OmniVoice prepares them (and transcribes the clip), Fish Speech reuses the stored clip and transcript.

## Web UI

```bash
sonorus serve                 # http://localhost:7860
sonorus serve --host 0.0.0.0  # share on your network
```

- **Ctrl + Enter** casts the spell.
- Pick your house in the top-right to change the theme.
- The **Copy as CLI command** button turns whatever you just made into a `sonorus speak …` command.
- "Fixed seed" + the same settings reproduces the same voice. Pin the seed of any result you liked.

## CLI

```bash
sonorus speak "Mischief managed." -o out.wav --play                 # Sorting Hat (auto voice)
sonorus speak "Hello there." -d "female, elderly, british accent"   # Transfiguration
sonorus speak "Hello there." -r clip.wav -t "What the clip says."   # one-off clone

sonorus clone minerva clip.wav -t "What the clip says."             # save to the vault
sonorus speak "Five points from Gryffindor." -v minerva --seed 7 --steps 16
sonorus speak "[whisper] Five points from Gryffindor." -v minerva -e fish --temperature 0.6   # Fish Speech
cat chapter1.txt | sonorus speak -v minerva -o chapter1.flac        # long text is chunked automatically

sonorus voices                # list the vault
sonorus forget minerva        # delete a voice
sonorus history               # recent generations
sonorus languages hindi       # search the 600+ languages
sonorus speak --help          # every option, including the advanced ones
```

If `sonorus serve` is running, CLI commands go through it (model already warm, results show up in
the UI's Pensieve). Otherwise the CLI loads the model itself. Use `--local` or `--server URL` to choose.

## Text tricks

- Sounds: `[laughter]`, `[sigh]`, `[surprise-oh]`, `[question-en]`, … (buttons above the text box)
- English pronunciation: CMU phonemes in brackets, e.g. `the [B EY1 S] guitar`
- Chinese pronunciation: pinyin with tone number, e.g. `打ZHE2`

## Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `SONORUS_DEVICE` | `auto` | `cuda`, `cuda:1`, `cpu`, `mps`, `xpu` |
| `SONORUS_DTYPE` | `auto` | `float16` on GPU, `float32` on CPU; or `bfloat16` |
| `SONORUS_ASR_DEVICE` | `auto` | Whisper (auto-transcription) goes to CPU on GPUs under 8 GB |
| `SONORUS_MODEL` | `k2-fsa/OmniVoice` | HF repo id or local path |
| `SONORUS_HOME` | `~/.sonorus` | Vault + Pensieve storage |
| `SONORUS_HOST` / `SONORUS_PORT` | `127.0.0.1` / `7860` | Server bind address |
| `SONORUS_URL` | derived | Where the CLI looks for a running server |
| `SONORUS_HISTORY_LIMIT` | `200` | Oldest memories are dropped past this |
| `SONORUS_FISH_URL` | unset | fish-speech API server, e.g. `http://127.0.0.1:8080`; enables the Fish Speech engine |
| `SONORUS_FISH_API_KEY` | unset | Bearer token, if that server runs with `--api-key` |
| `SONORUS_FISH_MODEL` | `S2-Pro` | Name shown in the UI for the fish checkpoint |
| `SONORUS_ENGINE` | `omnivoice` | Default engine for `sonorus speak` |

## API

`GET /api/status` (per-engine state under `engines`) · `GET /api/options` · `POST /api/speak` (multipart: `text`, `engine` (`omnivoice`/`fish`), `voice`, `ref_audio`,
`ref_text`, `instruct`, `language`, `speed`, `duration`, `seed`, `normalize_text`, `format`, `params` as JSON;
add `?wait=false` to get a job id back at once, then poll `GET /api/jobs/{id}`) ·
`GET|POST /api/voices` · `DELETE /api/voices/{id}` · `GET /api/history` · `GET /api/history/{id}/audio` ·
`DELETE /api/history/{id}`. Interactive docs at `/docs`.

## Notes

- Speed depends on hardware and on the reference clip length (attention cost grows with it, so keep clips 5–10 s).
  On pre-Volta GPUs (e.g. Quadro P1000) Sonorus switches PyTorch to math attention, which is ~3× faster there.
  On small GPUs the audio codec and Whisper run on the CPU automatically to avoid out-of-memory errors.
- Swift (16 steps) is ~2× faster than Balanced (32) but slightly less clear; use Balanced or Masterwork for final takes.

- Reference clips: clean speech in the same language as the target text gives the best clone; 3–10 s for OmniVoice,
  10–30 s for Fish Speech.
- Voice design is trained on English and Chinese; other languages may be unstable.
- The CLI reads WAV/FLAC/OGG reference clips directly; MP3/M4A need ffmpeg on the system.
  The browser UI converts any format it can play to WAV before uploading, so no ffmpeg needed there.
