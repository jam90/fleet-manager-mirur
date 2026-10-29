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
    """Keep the phrase audio (if enabled) so it can be re-transcribed and used as a benchmark case."""
    if not (settings.voice_log_dir and settings.voice_log_audio):
        return None
    suffix = Path(filename).suffix or mimetypes.guess_extension(content_type.split(";")[0]) or ".bin"
    rel = Path("audio") / f"{datetime.now():%Y%m%d-%H%M%S}-{event_id}{suffix}"
    path = Path(settings.voice_log_dir) / rel
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:  # logging must never break a voice request
        logger.warning("voice log: could not save audio: %s", exc)
        return None
    return str(rel)


def log_event(event_type: str, /, **fields) -> None:
    """Append one JSON line to voice.jsonl: voice phrases, orders sent and cancellations, in time order.

    `event_type` is positional-only so any field name (e.g. decide()'s "kind") can go in **fields.
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
        logger.warning("voice log: could not write %s: %s", path, exc)
