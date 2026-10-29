import httpx

from app.config import settings


class WhisperError(Exception):
    pass


async def transcribe(audio: bytes, filename: str, content_type: str) -> str:
    """Envía el audio al servidor whisper.cpp (/inference) y devuelve el texto.

    El servidor debe arrancar con --convert para aceptar webm/ogg/mp3 directamente.
    """
    files = {"file": (filename, audio, content_type)}
    data = {
        "response_format": "json",
        "temperature": "0.0",
        "language": settings.whisper_language,
        "prompt": settings.whisper_prompt,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.whisper_timeout) as client:
            resp = await client.post(settings.whisper_url, files=files, data=data)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise WhisperError(f"whisper request failed: {exc}") from exc
    return resp.json().get("text", "").strip()


async def is_healthy() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(settings.whisper_health_url)
        return resp.is_success
    except httpx.HTTPError:
        return False
