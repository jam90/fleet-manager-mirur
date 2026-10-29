import json
import logging
import mimetypes
import uuid
from datetime import datetime
from pathlib import Path

from app.config import settings

logger = logging.getLogger("uvicorn.error")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def save_audio(event_id: str, data: bytes, filename: str, content_type: str) -> str | None:
    """Guarda el audio de la frase (si está activado) para poder retranscribirlo y usarlo como caso de benchmark."""
    if not (settings.voice_log_dir and settings.voice_log_audio):
        return None
    suffix = Path(filename).suffix or mimetypes.guess_extension(content_type.split(";")[0]) or ".bin"
    rel = Path("audio") / f"{datetime.now():%Y%m%d-%H%M%S}-{event_id}{suffix}"
    path = Path(settings.voice_log_dir) / rel
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:  # el registro nunca debe romper una petición de voz
        logger.warning("registro de voz: no se pudo guardar el audio: %s", exc)
        return None
    return str(rel)


def log_event(event_type: str, /, **fields) -> None:
    """Añade una línea JSON a voice.jsonl: frases, orders enviadas y cancelaciones, en orden temporal.

    `event_type` es solo posicional para que cualquier nombre de campo (p. ej. "kind" de decide()) pueda ir en **fields.
    """
    if not settings.voice_log_dir:
        return
    path = Path(settings.voice_log_dir) / "voice.jsonl"
    record = {"ts": datetime.now().isoformat(timespec="milliseconds"), "type": event_type, **fields}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("registro de voz: no se pudo escribir %s: %s", path, exc)
