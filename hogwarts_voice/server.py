"""FastAPI server: JSON API + the Hogwarts-themed web UI."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Settings
from .engine import (
    AUDIO_FORMATS,
    DESIGN_ATTRIBUTES,
    GENERATION_PARAMS,
    NONVERBAL_TAGS,
    Engine,
    SonorusError,
    SpeakRequest,
    list_languages,
    normalization_available,
)
from .fish import FISH_PARAMS, FISH_TAGS

STATIC_DIR = Path(__file__).parent / "static"
MEDIA_TYPES = {"wav": "audio/wav", "flac": "audio/flac", "ogg": "audio/ogg"}
MAX_JOBS = 200  # finished speak jobs remembered for polling


def create_app(settings: Settings | None = None, preload: bool = True) -> FastAPI:
    engine = Engine(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if preload:
            engine.load_in_background()
        yield

    app = FastAPI(title="Sonorus", version=__version__, lifespan=lifespan)
    app.state.engine = engine
    # Speak jobs for clients behind proxies that cut long requests (RunPod's closes them after ~100 s).
    jobs: OrderedDict[str, dict] = OrderedDict()
    runner = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sonorus-job")

    @app.exception_handler(SonorusError)
    async def _user_error(_req, exc: SonorusError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    def _save_upload(upload: UploadFile | None, tmpdir: str) -> str | None:
        if upload is None or not upload.filename:
            return None
        dst = Path(tmpdir) / ("ref" + (Path(upload.filename).suffix or ".wav"))
        with dst.open("wb") as fh:
            shutil.copyfileobj(upload.file, fh)
        return str(dst)

    # -- meta ---------------------------------------------------------------

    @app.get("/api/status")
    def status():
        return engine.status()

    @app.post("/api/load")
    def load():
        if engine.state not in ("ready", "loading"):
            engine.load_in_background()
        return engine.status()

    @app.get("/api/options")
    def options():
        omni_features = {"design": True, "style_hints": True, "language": True, "speed": True, "duration": True,
                         "presets": True, "normalize": normalization_available()}
        return {
            "version": __version__,
            "params": GENERATION_PARAMS,
            "nonverbal": [{"tag": t, "label": l} for t, l in NONVERBAL_TAGS],
            "design": DESIGN_ATTRIBUTES,
            "formats": list(AUDIO_FORMATS),
            "languages": list_languages(),
            "normalization": omni_features["normalize"],
            # What the UI shows per engine. ref_seconds is the reference clip length each one likes best.
            "engines": [
                {"id": "omnivoice", "label": "OmniVoice", "available": True, "params": GENERATION_PARAMS,
                 "nonverbal": [{"tag": t, "label": l} for t, l in NONVERBAL_TAGS],
                 "features": omni_features, "ref_seconds": [3, 10]},
                {"id": "fish", "label": "Fish Speech", "available": engine.fish is not None, "params": FISH_PARAMS,
                 "nonverbal": [{"tag": t, "label": l} for t, l in FISH_TAGS],
                 "features": {k: False for k in omni_features}, "ref_seconds": [10, 30]},
            ],
        }

    # -- speech -------------------------------------------------------------

    @app.post("/api/speak")
    def speak(
        text: str = Form(...),
        voice: str | None = Form(None),
        ref_text: str | None = Form(None),
        instruct: str | None = Form(None),
        language: str | None = Form(None),
        speed: float | None = Form(None),
        duration: float | None = Form(None),
        seed: int | None = Form(None),
        normalize_text: bool = Form(False),
        format: str = Form("wav"),
        params: str = Form("{}"),
        engine_id: str = Form("omnivoice", alias="engine"),
        ref_audio: UploadFile | None = File(None),
        wait: bool = Query(True, description="false = return a job id at once and poll /api/jobs/{id}"),
    ):
        try:
            parsed = json.loads(params or "{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "params must be a JSON object")
        tmp = tempfile.mkdtemp(prefix="sonorus-")
        req = SpeakRequest(
            text=text, voice=voice or None, ref_audio=_save_upload(ref_audio, tmp), ref_text=ref_text,
            instruct=instruct, language=language or None, speed=speed, duration=duration, seed=seed,
            normalize_text=normalize_text, format=format, params=parsed, engine=engine_id,
        )

        def run() -> dict:
            try:
                item = engine.speak(req)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return {**item, "url": f"/api/history/{item['id']}/audio"}

        if wait:
            return run()
        job_id = uuid.uuid4().hex
        jobs[job_id] = {"state": "running"}
        while len(jobs) > MAX_JOBS:
            jobs.popitem(last=False)

        def finish(future):
            try:
                jobs[job_id] = {"state": "done", "item": future.result()}
            except SonorusError as exc:
                jobs[job_id] = {"state": "error", "detail": str(exc)}
            except Exception as exc:
                logging.getLogger("sonorus").exception("Speak job failed")
                jobs[job_id] = {"state": "error", "detail": f"The spell backfired: {exc}"}

        runner.submit(run).add_done_callback(finish)
        return JSONResponse(status_code=202, content={"job": job_id, "state": "running"})

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, "No such spell in progress (the server may have restarted).")
        return {"job": job_id, **jobs[job_id]}

    # -- vault --------------------------------------------------------------

    @app.get("/api/voices")
    def voices():
        return engine.vault.list()

    @app.post("/api/voices")
    def create_voice(
        name: str = Form(...),
        ref_audio: UploadFile = File(...),
        ref_text: str | None = Form(None),
        description: str = Form(""),
        preprocess: bool = Form(True),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = _save_upload(ref_audio, tmp)
            if path is None:
                raise HTTPException(400, "Upload a reference clip to create a voice.")
            return engine.create_voice(name, path, ref_text, description, preprocess)

    @app.get("/api/voices/{voice_id}/sample")
    def voice_sample(voice_id: str):
        meta = engine.vault.get(voice_id)
        path = engine.vault.reference_path(meta["id"])
        if not path.exists():
            raise HTTPException(404, "No stored reference clip for this voice.")
        return FileResponse(path, media_type="audio/wav")

    @app.delete("/api/voices/{voice_id}")
    def delete_voice(voice_id: str):
        return engine.vault.delete(voice_id)

    # -- pensieve -----------------------------------------------------------

    @app.get("/api/history")
    def history():
        return [{**i, "url": f"/api/history/{i['id']}/audio"} for i in engine.pensieve.list()]

    @app.get("/api/history/{item_id}/audio")
    def history_audio(item_id: str, download: bool = False):
        item = engine.pensieve.get(item_id)
        path = engine.pensieve.audio_path(item)
        if not path.exists():
            raise HTTPException(404, "The audio for this memory has faded.")
        name = f"sonorus-{item_id}.{item['format']}" if download else None
        return FileResponse(path, media_type=MEDIA_TYPES[item["format"]], filename=name)

    @app.delete("/api/history/{item_id}")
    def delete_history(item_id: str):
        engine.pensieve.delete(item_id)
        return {"deleted": item_id}

    @app.delete("/api/history")
    def clear_history():
        return {"deleted": engine.pensieve.clear()}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app


def run(host: str | None = None, port: int | None = None, preload: bool = True) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    if host:
        settings.host = host
    if port:
        settings.port = port
    app = create_app(settings, preload=preload)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
