"""`sonorus` command-line interface.

If a Sonorus server is running (see `sonorus serve`), commands go through it, so the model is
already warm and results show up in the web UI's Pensieve. Otherwise the model is loaded in-process.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import server_url

# Kept in sync with engine.GENERATION_PARAMS, duplicated here so `--help` doesn't import torch.
PARAM_FLAGS = {
    "num_step": (int, ["--steps"], "Diffusion steps (default 32; 16 is faster)."),
    "guidance_scale": (float, ["--guidance"], "Classifier-free guidance scale (default 2.0)."),
    "t_shift": (float, ["--t-shift"], "Noise-schedule time-step shift (default 0.1)."),
    "position_temperature": (float, ["--position-temp"], "Mask-position temperature, 0 = deterministic (default 5.0)."),
    "class_temperature": (float, ["--token-temp"], "Token sampling temperature, 0 = greedy (default 0.0)."),
    "layer_penalty_factor": (float, ["--layer-penalty"], "Deeper codebook layer penalty (default 5.0)."),
    "pad_duration": (float, ["--pad"], "Silence padding per side, seconds (default 0.1)."),
    "fade_duration": (float, ["--fade"], "Fade in/out length, seconds (default 0.1)."),
    "audio_chunk_duration": (float, ["--chunk-duration"], "Long-form chunk length, seconds (default 15)."),
    "audio_chunk_threshold": (float, ["--chunk-threshold"], "Chunk text longer than this many seconds (default 30)."),
    "denoise": (bool, ["--denoise"], "Ask for cleaner speech (default on)."),
    "preprocess_prompt": (bool, ["--preprocess"], "Clean the reference clip and transcript (default on)."),
    "postprocess_output": (bool, ["--postprocess"], "Trim long silences from the output (default on)."),
}

GOLD, GREEN, RED, DIM, BOLD, RESET = "\033[33m", "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
if not sys.stderr.isatty() or os.environ.get("NO_COLOR"):
    GOLD = GREEN = RED = DIM = BOLD = RESET = ""


def say(msg: str) -> None:
    print(msg, file=sys.stderr)


def fail(msg: str) -> None:
    say(f"{RED}✗ {msg}{RESET}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Backends: talk to a running server, or load the model in-process
# ---------------------------------------------------------------------------


class RemoteBackend:
    def __init__(self, url: str):
        import httpx

        self.url = url
        self.http = httpx.Client(base_url=url, timeout=None)

    @staticmethod
    def probe(url: str) -> bool:
        import httpx

        try:
            return httpx.get(f"{url}/api/status", timeout=0.7).status_code == 200
        except httpx.HTTPError:
            return False

    def _check(self, resp):
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except Exception:
                detail = resp.text
            fail(str(detail))
        return resp

    def wait_ready(self):
        import time

        shown = False
        while True:
            st = self.http.get("/api/status").json()
            if st["state"] == "ready":
                return
            if st["state"] == "error":
                fail(st["message"])
            if st["state"] == "idle":
                self.http.post("/api/load")
            if not shown:
                say(f"{DIM}… {st['message']}{RESET}")
                shown = True
            time.sleep(1.5)

    def speak(self, req: dict, output: Path | None) -> tuple[dict, Path]:
        self.wait_ready()
        data = {k: v for k, v in req.items() if k not in ("ref_audio", "params") and v is not None}
        data["params"] = json.dumps(req.get("params") or {})
        if req.get("ref_audio"):
            with open(req["ref_audio"], "rb") as fh:
                resp = self.http.post("/api/speak", data=data, files={"ref_audio": (Path(req["ref_audio"]).name, fh)})
        else:
            resp = self.http.post("/api/speak", data=data)
        item = self._check(resp).json()
        out = output or Path(f"sonorus-{item['id']}.{item['format']}")
        out.write_bytes(self._check(self.http.get(item["url"])).content)
        return item, out

    def clone(self, name, audio, text, description) -> dict:
        self.wait_ready()
        with open(audio, "rb") as fh:
            return self._check(self.http.post(
                "/api/voices",
                data={"name": name, "ref_text": text or "", "description": description or ""},
                files={"ref_audio": fh},
            )).json()

    def voices(self) -> list[dict]:
        return self._check(self.http.get("/api/voices")).json()

    def forget(self, name) -> dict:
        voices = self.voices()
        match = next((v for v in voices if name.lower() in (v["id"], v["name"].lower())), None)
        if not match:
            fail(f"No voice called '{name}' in the vault.")
        return self._check(self.http.delete(f"/api/voices/{match['id']}")).json()

    def history(self) -> list[dict]:
        return self._check(self.http.get("/api/history")).json()


class LocalBackend:
    def __init__(self):
        from .engine import Engine

        say(f"{DIM}No Sonorus server found at {server_url()}; loading the model in this process."
            f" (Tip: run `sonorus serve` once to keep it warm.){RESET}")
        self.engine = Engine()

    def speak(self, req: dict, output: Path | None) -> tuple[dict, Path]:
        from .engine import request_from_dict

        item = self.engine.speak(request_from_dict(req))
        src = self.engine.pensieve.audio_path(item)
        out = output or Path(f"sonorus-{item['id']}.{item['format']}")
        shutil.copyfile(src, out)
        return item, out

    def clone(self, name, audio, text, description) -> dict:
        return self.engine.create_voice(name, audio, text, description or "")

    def voices(self) -> list[dict]:
        return self.engine.vault.list()

    def forget(self, name) -> dict:
        return self.engine.vault.delete(name)

    def history(self) -> list[dict]:
        return self.engine.pensieve.list()


def backend(args):
    if not getattr(args, "local", False):
        url = (args.server or server_url()).rstrip("/")
        if RemoteBackend.probe(url):
            return RemoteBackend(url)
        if args.server:
            fail(f"Could not reach a Sonorus server at {url}")
    return LocalBackend()


def guard(fn):
    """Turn SonorusError into a friendly message instead of a traceback."""
    def wrapper(args):
        try:
            return fn(args)
        except Exception as exc:
            if type(exc).__name__ == "SonorusError":
                fail(str(exc))
            raise
    return wrapper


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _read_text(args) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.text == "-" or (args.text is None and not sys.stdin.isatty()):
        return sys.stdin.read()
    if args.text is None:
        fail('Give me something to say, e.g. sonorus speak "Mischief managed."')
    return args.text


def _play(path: Path) -> None:
    for player in (["paplay"], ["aplay", "-q"], ["afplay"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        if shutil.which(player[0]):
            subprocess.run([*player, str(path)], check=False)
            return
    say(f"{DIM}No audio player found (paplay/aplay/afplay/ffplay); skipping playback.{RESET}")


@guard
def cmd_speak(args):
    text = _read_text(args)
    fmt = args.format
    if args.output and not fmt:
        ext = Path(args.output).suffix.lstrip(".").lower()
        fmt = ext if ext in ("wav", "flac", "ogg") else None
    if args.ref_audio and not Path(args.ref_audio).exists():
        fail(f"Reference audio not found: {args.ref_audio}")
    params = {k: getattr(args, k) for k in PARAM_FLAGS if getattr(args, k) is not None}
    req = {
        "text": text, "voice": args.voice, "ref_audio": args.ref_audio, "ref_text": args.ref_text,
        "instruct": args.design, "language": args.lang, "speed": args.speed, "duration": args.duration,
        "seed": args.seed, "normalize_text": args.normalize, "format": fmt or "wav", "params": params,
    }
    mode = "clone" if (args.voice or args.ref_audio) else "design" if args.design else "auto"
    who = args.voice or (Path(args.ref_audio).name if args.ref_audio else args.design or "a voice of the model's choosing")
    say(f"{GOLD}🪄 Sonorus!{RESET} {DIM}({mode}: {who}){RESET}")
    item, out = backend(args).speak(req, Path(args.output) if args.output else None)
    say(f"{GREEN}✓{RESET} {BOLD}{out}{RESET}  {item['seconds']:.1f}s of speech in {item['elapsed']:.1f}s"
        f" {DIM}(seed {item['seed']}, {item.get('device') or 'server'}){RESET}")
    print(out)
    if args.play:
        _play(out)


@guard
def cmd_clone(args):
    if not Path(args.audio).exists():
        fail(f"Reference audio not found: {args.audio}")
    say(f"{GOLD}🧪 Brewing Polyjuice for '{args.name}'…{RESET}"
        + ("" if args.text else f" {DIM}(no transcript given; Whisper will transcribe it){RESET}"))
    meta = backend(args).clone(args.name, args.audio, args.text, args.description)
    say(f"{GREEN}✓{RESET} Voice {BOLD}{meta['name']}{RESET} stored in the vault as '{meta['id']}'.")
    say(f"  {DIM}Transcript: {meta['ref_text']}{RESET}")
    say(f'  Use it: sonorus speak "Hello there" --voice {meta["id"]}')


@guard
def cmd_voices(args):
    voices = backend(args).voices()
    if args.json:
        print(json.dumps(voices, indent=2, ensure_ascii=False))
        return
    if not voices:
        say("The vault is empty. Add one with: sonorus clone NAME clip.wav")
        return
    for v in voices:
        desc = f" — {v['description']}" if v.get("description") else ""
        print(f"{BOLD}{v['id']:<24}{RESET} {v['name']}{desc}")
        print(f"  {DIM}{v['ref_text'][:90]}{RESET}")


@guard
def cmd_forget(args):
    meta = backend(args).forget(args.name)
    say(f"{GREEN}✓{RESET} Obliviated '{meta['name']}' from the vault.")


@guard
def cmd_history(args):
    items = backend(args).history()[: args.limit]
    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return
    for i in items:
        who = i.get("voice_name") or i.get("instruct") or i["mode"]
        print(f"{DIM}{i['created'][:19].replace('T', ' ')}{RESET}  {i['seconds']:>5.1f}s  {GOLD}{who:<20.20}{RESET}  {i['text'][:60]!r}")


def cmd_languages(args):
    from omnivoice.utils.lang_map import LANG_NAME_TO_ID

    q = (args.query or "").lower()
    rows = sorted((name, code) for name, code in LANG_NAME_TO_ID.items() if q in name or q == code)
    for name, code in rows:
        print(f"{code:<8} {name.title()}")
    say(f"{DIM}{len(rows)} language(s){RESET}")


def cmd_serve(args):
    from .server import run

    host = args.host or os.environ.get("SONORUS_HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("SONORUS_PORT", "7860"))
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    say(f"{GOLD}🏰 The Sonorus Chamber is open at http://{shown}:{port}{RESET}  {DIM}(Ctrl+C to close){RESET}")
    run(host=host, port=port, preload=not args.lazy)


def cmd_status(args):
    url = (args.server or server_url()).rstrip("/")
    if not RemoteBackend.probe(url):
        say(f"No server at {url}. Start one with: sonorus serve")
        sys.exit(1)
    import httpx

    st = httpx.get(f"{url}/api/status").json()
    print(json.dumps(st, indent=2))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sonorus",
        description="Hogwarts-themed voice cloning and text-to-speech, powered by OmniVoice.",
        epilog='Examples:\n'
               '  sonorus serve                                   # open the web UI\n'
               '  sonorus speak "Mischief managed." -o out.wav    # auto voice\n'
               '  sonorus speak "Hello" -d "female, british accent, low pitch"\n'
               '  sonorus clone minerva clip.wav --text "What the clip says."\n'
               '  sonorus speak "Five points from Gryffindor." -v minerva --play\n'
               '  cat story.txt | sonorus speak -v minerva -o story.flac',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"sonorus {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def conn(sp):
        sp.add_argument("--server", metavar="URL", help="Sonorus server URL (default $SONORUS_URL or http://127.0.0.1:7860).")
        sp.add_argument("--local", action="store_true", help="Ignore any running server and load the model in-process.")

    s = sub.add_parser("speak", help="Turn text into speech.", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    s.add_argument("text", nargs="?", help="Text to speak. Use '-' (or pipe) to read stdin.")
    s.add_argument("-f", "--file", help="Read the text from a file.")
    s.add_argument("-o", "--output", help="Output file (.wav/.flac/.ogg). Default: ./sonorus-<id>.wav")
    g = s.add_argument_group("voice (pick one; none = the model chooses)")
    g.add_argument("-v", "--voice", help="Saved voice from the vault (id or name).")
    g.add_argument("-r", "--ref-audio", help="Clone a one-off voice from this 3-10 s clip.")
    g.add_argument("-t", "--ref-text", help="Transcript of --ref-audio (omit to auto-transcribe).")
    g.add_argument("-d", "--design", "--instruct", dest="design",
                   help='Voice design attributes, e.g. "male, elderly, low pitch, british accent". '
                        "Can also be combined with a cloned voice.")
    o = s.add_argument_group("delivery")
    o.add_argument("-l", "--lang", help="Language name or code (e.g. English, en, hi). Default: auto.")
    o.add_argument("-s", "--speed", type=float, help="Speed factor, >1 faster, <1 slower.")
    o.add_argument("--duration", type=float, help="Exact output length in seconds (overrides --speed).")
    o.add_argument("--seed", type=int, help="Random seed for reproducible output.")
    o.add_argument("--normalize", action="store_true", help="Spell out numbers (needs omnivoice[tn]).")
    o.add_argument("--format", choices=["wav", "flac", "ogg"], help="Audio format (default from -o, else wav).")
    o.add_argument("--play", action="store_true", help="Play the result when done.")
    adv = s.add_argument_group("advanced generation options (defaults are OmniVoice's)")
    for key, (typ, flags, help_) in PARAM_FLAGS.items():
        if typ is bool:
            adv.add_argument(*flags, dest=key, action=argparse.BooleanOptionalAction, default=None, help=help_)
        else:
            adv.add_argument(*flags, dest=key, type=typ, default=None, help=help_)
    conn(s)
    s.set_defaults(func=cmd_speak)

    c = sub.add_parser("clone", help="Save a voice to the vault from a short clip.")
    c.add_argument("name", help="Name for the voice.")
    c.add_argument("audio", help="Reference clip, ideally 3-10 s of clean speech.")
    c.add_argument("-t", "--text", help="What is said in the clip (omit to auto-transcribe with Whisper).")
    c.add_argument("--description", help="A note to remember the voice by.")
    conn(c)
    c.set_defaults(func=cmd_clone)

    v = sub.add_parser("voices", help="List voices in the vault.")
    v.add_argument("--json", action="store_true")
    conn(v)
    v.set_defaults(func=cmd_voices)

    fg = sub.add_parser("forget", help="Delete a voice from the vault.")
    fg.add_argument("name")
    conn(fg)
    fg.set_defaults(func=cmd_forget)

    h = sub.add_parser("history", help="Show recent generations (the Pensieve).")
    h.add_argument("-n", "--limit", type=int, default=15)
    h.add_argument("--json", action="store_true")
    conn(h)
    h.set_defaults(func=cmd_history)

    lg = sub.add_parser("languages", help="List or search the 600+ supported languages.")
    lg.add_argument("query", nargs="?")
    lg.set_defaults(func=cmd_languages)

    sv = sub.add_parser("serve", help="Start the web UI and API.")
    sv.add_argument("--host", help="Bind address (default 127.0.0.1; use 0.0.0.0 to share on your network).")
    sv.add_argument("--port", type=int, help="Port (default 7860).")
    sv.add_argument("--lazy", action="store_true", help="Load the model on first use instead of at startup.")
    sv.set_defaults(func=cmd_serve)

    st = sub.add_parser("status", help="Show the running server's status.")
    st.add_argument("--server", metavar="URL")
    st.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        fail("Interrupted.")


if __name__ == "__main__":
    main()
