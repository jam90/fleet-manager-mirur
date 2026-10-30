import json
import re
import unicodedata
from pathlib import Path

from app.config import settings


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def is_word(word: str, target: str, variants: list[str] = ()) -> bool:
    """Coincidencia tolerante: Whisper a veces escribe mal las palabras de activación/confirmación."""
    target = normalize(target)
    return levenshtein(word, target) <= 1 or word in {normalize(v) for v in variants}


def load_missions() -> dict[str, dict]:
    """Lee la tabla de misiones en cada llamada para que los cambios se apliquen sin reiniciar."""
    raw = json.loads(Path(settings.missions_file).read_text(encoding="utf-8"))
    for mission in raw.values():
        mission["words"] = {normalize(w) for w in mission["words"]}
        mission.setdefault("params", {})
    return raw


def match_mission(words: list[str], missions: dict[str, dict]) -> tuple[str, str] | tuple[None, None]:
    """Devuelve (misión, palabra) para la primera palabra de la transcripción que dispara una misión."""
    for word in words:
        for name, mission in missions.items():
            if word in mission["words"]:
                return name, word
    return None, None


def decide(transcription: str) -> dict:
    normalized = normalize(transcription)
    words = normalized.split()
    wake = bool(words) and is_word(words[0], settings.wake_word, settings.wake_variants)
    command_words = words[1:] if wake else words
    missions = load_missions()
    mission, word = match_mission(command_words, missions)
    entry = missions[mission] if mission else {}
    return {
        "transcription": transcription,
        "normalized": normalized,
        "wake": wake,
        "command": " ".join(command_words),
        "confirm": any(is_word(w, settings.confirm_word) for w in words),
        "mission": mission,
        "matched_word": word,
        "kind": ("stop" if "instant" in entry else "mission") if mission else None,
        "actionType": entry.get("actionType"),
    }
