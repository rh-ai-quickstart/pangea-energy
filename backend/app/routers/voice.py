"""Speech-to-text proxy: forwards recorded audio to the in-cluster Whisper
predictor (OpenAI-compatible /v1/audio/transcriptions) so the UI never reaches
the model service directly."""
from __future__ import annotations

import httpx
from fastapi import APIRouter, File, HTTPException, Response, UploadFile

from ..config import settings
from ..models import SpeakRequest

router = APIRouter(prefix="/api/voice", tags=["voice"])


@router.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)) -> dict:
    """Transcribe an uploaded audio clip to text via the Whisper model."""
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio upload.")
    if len(data) > settings.whisper_max_upload_bytes:
        raise HTTPException(status_code=413, detail="Audio clip too large.")

    files = {
        "file": (audio.filename or "clip.webm", data, audio.content_type or "audio/webm"),
    }
    payload = {"model": settings.whisper_model}
    if settings.whisper_language:
        payload["language"] = settings.whisper_language

    try:
        async with httpx.AsyncClient(timeout=settings.whisper_http_timeout) as client:
            resp = await client.post(settings.whisper_url, data=payload, files=files)
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Transcription service returned {exc.response.status_code}.",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail="Transcription service unreachable."
        ) from exc

    return {"text": (body.get("text") or "").strip()}


@router.post("/speak")
async def speak(req: SpeakRequest) -> Response:
    """Synthesize `text` to speech via the pangea-tts gateway and return WAV audio."""
    if not settings.tts_enabled:
        raise HTTPException(status_code=503, detail="Text-to-speech is disabled.")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text.")
    text = text[: settings.kokoro_max_chars]

    payload = {
        "text": text,
        "voice": req.voice or settings.kokoro_voice,
        "lang": settings.kokoro_lang,
        "speed": settings.kokoro_speed,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.kokoro_http_timeout) as client:
            resp = await client.post(settings.tts_gateway_url, json=payload)
            resp.raise_for_status()
            audio = resp.content
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Speech service returned {exc.response.status_code}.",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Speech service unreachable.") from exc

    if not audio:
        raise HTTPException(status_code=502, detail="Speech service returned no audio.")

    return Response(content=audio, media_type="audio/wav")
