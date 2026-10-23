from __future__ import annotations

import io
import logging
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from config import settings
from kokoro_remote import RemoteSession

log = logging.getLogger("pangea-tts")

_kokoro = None


class SpeakRequest(BaseModel):
    text: str
    voice: str | None = None
    lang: str | None = None
    speed: float | None = None


def _build_kokoro():
    from kokoro_onnx import Kokoro
    from kokoro_onnx.config import get_vocab

    placeholder = Path(settings.placeholder_path)
    placeholder.parent.mkdir(parents=True, exist_ok=True)
    placeholder.touch(exist_ok=True)

    with httpx.Client(timeout=settings.http_timeout) as client:
        resp = client.get(settings.metadata_url)
        resp.raise_for_status()
        metadata = resp.json()

    session = RemoteSession(
        infer_url=settings.infer_url,
        metadata=metadata,
        model_path=str(placeholder),
        timeout=settings.http_timeout,
    )
    return Kokoro.from_session(
        session, settings.voices_path, vocab_config={"vocab": get_vocab()}
    )


def _get_kokoro():
    global _kokoro
    if _kokoro is None:
        _kokoro = _build_kokoro()
    return _kokoro


def _synth(text: str, voice: str, lang: str, speed: float) -> bytes:
    kokoro = _get_kokoro()
    samples, sample_rate = kokoro.create(text, voice=voice, speed=speed, lang=lang)
    pcm = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate))
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _kokoro
    try:
        _kokoro = await run_in_threadpool(_build_kokoro)
    except Exception as exc:
        log.warning("Kokoro warm load deferred: %s", exc)
        _kokoro = None
    yield


app = FastAPI(title="Pangea TTS gateway", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "model_loaded": _kokoro is not None}


@app.post("/speak")
async def speak(req: SpeakRequest) -> Response:
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text.")
    text = text[: settings.max_chars]
    voice = req.voice or settings.voice
    lang = req.lang or settings.lang
    speed = req.speed if req.speed is not None else settings.speed

    try:
        wav_bytes = await run_in_threadpool(_synth, text, voice, lang, speed)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="TTS model server unreachable.") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"TTS synthesis failed: {exc}") from exc

    return Response(content=wav_bytes, media_type="audio/wav")
