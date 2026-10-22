# Sonorus 🪄

A Hogwarts-themed voice cloning and text-to-speech service built on
[OmniVoice](https://github.com/k2-fsa/OmniVoice) (600+ languages, zero-shot cloning, voice design).
You get a web UI with every OmniVoice option, plus a `sonorus` CLI for scripting.

| Spell | What it does |
|---|---|
| 🧪 **Polyjuice Potion** | Clone a voice from a 3–10 s clip (upload, drag-drop or record in the browser) |
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

## API

`GET /api/status` · `GET /api/options` · `POST /api/speak` (multipart: `text`, `voice`, `ref_audio`,
`ref_text`, `instruct`, `language`, `speed`, `duration`, `seed`, `normalize_text`, `format`, `params` as JSON) ·
`GET|POST /api/voices` · `DELETE /api/voices/{id}` · `GET /api/history` · `GET /api/history/{id}/audio` ·
`DELETE /api/history/{id}`. Interactive docs at `/docs`.

## Notes

- Speed depends on hardware and on the reference clip length (attention cost grows with it, so keep clips 5–10 s).
  On pre-Volta GPUs (e.g. Quadro P1000) Sonorus switches PyTorch to math attention, which is ~3× faster there.
  On small GPUs the audio codec and Whisper run on the CPU automatically to avoid out-of-memory errors.
- Swift (16 steps) is ~2× faster than Balanced (32) but slightly less clear; use Balanced or Masterwork for final takes.

- Reference clips: 3–10 s of clean speech in the same language as the target text gives the best clone.
- Voice design is trained on English and Chinese; other languages may be unstable.
- The CLI reads WAV/FLAC/OGG reference clips directly; MP3/M4A need ffmpeg on the system.
  The browser UI converts any format it can play to WAV before uploading, so no ffmpeg needed there.
