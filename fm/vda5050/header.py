"""Header VDA 5050 y su relación con el topic MQTT.

Regla central (CLAUDE.md §5.1): en todo mensaje, `manufacturer` y
`serialNumber` del payload coinciden carácter a carácter con los segmentos
del topic `vda5050/v3/<manufacturer>/<serialNumber>/<subtopic>`. Aquí viven
las tres cosas que lo garantizan: construir topics, construir headers y
validar mensajes entrantes. Los scripts de prueba usan estas mismas funciones
para que no puedan divergir del servicio.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

INTERFACE = "vda5050"
MAJOR = "v3"
VERSION = "3.0.0"

# Subtopics que maneja el FM. `order_response` es extensión propia (§5.7).
SUBTOPICS = ("order", "instantActions", "state", "connection", "factsheet",
             "visualization", "order_response")


def now_iso() -> str:
    """`2026-09-16T10:30:00.123Z` — ISO 8601 UTC con milisegundos (§7.2)."""
    return (datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))


def topic(manufacturer: str, serial: str, subtopic: str) -> str:
    return f"{INTERFACE}/{MAJOR}/{manufacturer}/{serial}/{subtopic}"


@dataclass(frozen=True)
class ParsedTopic:
    manufacturer: str
    serial: str
    subtopic: str


def parse_topic(t: str) -> ParsedTopic | None:
    """Devuelve los segmentos del topic o None si no tiene la forma VDA."""
    parts = t.split("/")
    if len(parts) != 5 or parts[0] != INTERFACE or parts[1] != MAJOR:
        return None
    if not parts[2] or not parts[3] or not parts[4]:
        return None
    return ParsedTopic(parts[2], parts[3], parts[4])


class HeaderCounter:
    """`headerId` monótono por topic (§7.2). Arranca en 1 en cada arranque.
    Con Lock: lo usan el hilo principal y el servidor web (`fm/web.py`)."""

    def __init__(self) -> None:
        self._next: dict[str, int] = {}
        self._lock = threading.Lock()

    def next(self, topic_name: str) -> int:
        with self._lock:
            n = self._next.get(topic_name, 1)
            self._next[topic_name] = n + 1
            return n


def make_header(counter: HeaderCounter, manufacturer: str, serial: str,
                subtopic: str) -> dict:
    """Header completo para publicar en `topic(manufacturer, serial, subtopic)`."""
    return {
        "headerId": counter.next(topic(manufacturer, serial, subtopic)),
        "timestamp": now_iso(),
        "version": VERSION,
        "manufacturer": manufacturer,
        "serialNumber": serial,
    }


def validate_header(topic_name: str, payload: object) -> str | None:
    """Comprueba header ≡ topic en un mensaje entrante.

    Devuelve None si es válido o una descripción del fallo (irá a
    `errorDescription` de un `VALIDATION_FAILURE`). No usa el schema: esto
    es la comprobación mínima que se hace siempre, con o sin `--validate`.
    """
    if not isinstance(payload, dict):
        return "el payload no es un objeto JSON"
    pt = parse_topic(topic_name)
    if pt is None:
        return f"topic '{topic_name}' no tiene la forma {INTERFACE}/{MAJOR}/<manufacturer>/<serialNumber>/<subtopic>"
    for k in ("headerId", "timestamp", "version", "manufacturer", "serialNumber"):
        if k not in payload:
            return f"falta el campo de header '{k}'"
    if not isinstance(payload["headerId"], int) or isinstance(payload["headerId"], bool):
        return "headerId debe ser un entero"
    if payload["manufacturer"] != pt.manufacturer:
        return (f"manufacturer del header ('{payload['manufacturer']}') no coincide con "
                f"el del topic ('{pt.manufacturer}')")
    if payload["serialNumber"] != pt.serial:
        return (f"serialNumber del header ('{payload['serialNumber']}') no coincide con "
                f"el del topic ('{pt.serial}')")
    version = str(payload["version"])
    if not version.startswith(MAJOR[1:] + "."):
        return f"version '{version}' no es de la familia {MAJOR}"
    return None
