"""Misiones de UN MiR para la pestaña Robot (docs/avance.md, decisión 113).

Qué está haciendo el robot y qué ha hecho, lo lanzara el FM o no (missions
desde la web o la tablet también salen): la entrada de `mission_queue` en
curso con sus pasos, las que esperan en cola y las últimas terminadas.

Solo lectura. Corre en el hilo de la web cuando la pestaña lo pide (cada
pocos segundos y solo para el robot que se está mirando), con su propio
`MirClient`: nunca comparte conexión con el bucle de 1 Hz.

Coste para el robot: `GET /mission_queue` trae toda la historia (1.482
entradas en mir-1 el 2026-10-23), pero cada una solo con id y estado, ~0,1 s.
El detalle de una entrada terminada no cambia, así que se pide una vez y se
guarda; el de la que está en curso y sus pasos se piden en cada consulta.

Las horas son las del reloj del robot, que puede ir desfasado (mir-1 2 min y
mir-2 5 min atrasados el 2026-10-23): con la cabecera `Date` de la respuesta
se calcula el desfase y va en `clock_offset_s` para que la UI corrija.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime

from fm.adapters.base import MissionRun, MissionStep, MissionsView
from fm.adapters.mir.client import MirClient
from fm.adapters.mir.translate import render_message

# Estado de una entrada de `mission_queue` → palabras de `actionStatus` VDA.
# Igual que `QUEUE_TO_JOB` del driver, más la pausa ("Paused" solo se ve aquí:
# el driver no la necesita porque una mission en pausa sigue viva).
QUEUE_STATUS = {
    "Pending": "WAITING", "Executing": "RUNNING", "Paused": "PAUSED", "Done": "FINISHED",
    "Aborted": "FAILED", "Cancelled": "FAILED", "Canceled": "FAILED",
}
FINAL = {"Done", "Aborted", "Cancelled", "Canceled"}
# Estado de un paso. "Succeded" (sic) es como lo escribe el MiR.
STEP_STATUS = {
    "Pending": "WAITING", "Executing": "RUNNING", "Paused": "PAUSED",
    "Succeded": "FINISHED", "Succeeded": "FINISHED", "Done": "FINISHED",
    "Failed": "FAILED", "Aborted": "FAILED", "Cancelled": "FAILED", "Canceled": "FAILED",
}
MAX_PENDING = 10        # una cola más larga no cabe en la pestaña ni es lo normal


def status_of(state: str, table: dict[str, str]) -> str:
    """Palabra VDA o, si el MiR dice otra cosa, su texto tal cual (mejor
    enseñar "Abort" que perderlo)."""
    return table.get(state, state)


def to_utc(ts) -> str | None:
    """Hora del MiR → ISO 8601 UTC con `Z`. Las entradas la traen con
    `+00:00`; los pasos, sin zona, pero es la misma hora (comprobado en mir-1:
    un paso empieza en el mismo segundo que su entrada)."""
    if not ts:
        return None
    ts = str(ts)
    if ts.endswith("+00:00"):
        return ts[:-6] + "Z"
    if ts.endswith("Z") or "+" in ts[10:] or "-" in ts[10:]:
        return ts
    return ts + "Z"


def clock_offset(date_header: str | None, now: float) -> float | None:
    """Cabecera HTTP `Date` del robot → su reloj menos el nuestro, en s.
    Resolución de 1 s: basta para corregir horas y duraciones de misiones."""
    if not date_header:
        return None
    try:
        return parsedate_to_datetime(date_header).timestamp() - now
    except (TypeError, ValueError):
        return None


def run_from_detail(d: dict, name: str, own: bool) -> MissionRun:
    """`GET /mission_queue/<id>` → `MissionRun` (sin pasos)."""
    params = [(str(p.get("id", "")), str(p.get("label") or p.get("value", "")))
              for p in d.get("parameters") or []]
    return MissionRun(job_id=str(d["id"]), name=name, status=status_of(str(d.get("state", "")), QUEUE_STATUS),
                      own=own, message=render_message(d.get("message")),
                      ordered=to_utc(d.get("ordered")), started=to_utc(d.get("started")),
                      finished=to_utc(d.get("finished")), parameters=params)


def step_from_detail(d: dict) -> MissionStep:
    """`GET /mission_queue/<id>/actions/<n>` → `MissionStep`."""
    return MissionStep(kind=str(d.get("action_type", "")),
                       status=status_of(str(d.get("state", "")), STEP_STATUS),
                       message=render_message(d.get("message")),
                       started=to_utc(d.get("started")), finished=to_utc(d.get("finished")))


def split_queue(entries: list[dict], history: int) -> tuple[int | None, list[int], list[int]]:
    """Cola completa (`id`, `state`) → (en curso, en cola, últimas terminadas).

    En curso: la más reciente que ni espera ni ha terminado (Executing,
    Paused…). En cola: las Pending, la más antigua primero (es la que va a
    salir). Historial: las `history` terminadas más recientes, la última primero."""
    ordered = sorted(entries, key=lambda e: int(e["id"]))
    live = [int(e["id"]) for e in ordered if e.get("state") not in FINAL and e.get("state") != "Pending"]
    pending = [int(e["id"]) for e in ordered if e.get("state") == "Pending"][:MAX_PENDING]
    done = [int(e["id"]) for e in ordered if e.get("state") in FINAL]
    return (live[-1] if live else None), pending, list(reversed(done[-history:])) if history > 0 else []


class MirMissions:
    """Fuente de misiones de un MiR (`MissionsSource` de fm/adapters/base.py)."""

    def __init__(self, client: MirClient, is_own: Callable[[int], bool] = lambda _: False):
        self.client = client
        self.is_own = is_own              # ¿la lanzó este FM? (lo sabe el driver)
        self._lock = threading.Lock()     # dos navegadores a la vez no pisan las cachés
        self._names: dict[str, str] = {}              # GUID de mission → nombre
        self._finished: dict[int, MissionRun] = {}    # entradas terminadas: no cambian
        self._steps: dict[tuple[int, int], MissionStep] = {}   # pasos terminados

    def _name(self, guid: str) -> str:
        if guid not in self._names:
            try:
                self._names[guid] = str(self.client.missions_mission_id_get(guid).get("name") or guid)
            except Exception:
                return guid        # sin caché: se reintenta en la próxima consulta
        return self._names[guid]

    def _run(self, qid: int) -> MissionRun:
        d = self.client.mission_queue_id_get(qid)
        return run_from_detail(d, self._name(str(d.get("mission_id", ""))), self.is_own(qid))

    def _steps_of(self, qid: int) -> list[MissionStep]:
        steps = []
        for a in self.client.mission_queue_id_actions_get(qid) or []:
            key = (qid, int(a["id"]))
            step = self._steps.get(key)
            if step is None:
                step = step_from_detail(self.client.mission_queue_id_actions_id_get(qid, key[1]))
                if step.status in ("FINISHED", "FAILED"):
                    self._steps[key] = step
            steps.append(step)
        return steps

    def view(self, history: int = 10) -> MissionsView:
        with self._lock:
            entries = self.client.mission_queue_get()
            offset = clock_offset(getattr(self.client, "last_date", None), time.time())
            current_id, pending_ids, done_ids = split_queue(entries, history)
            current = None
            if current_id is not None:
                current = self._run(current_id)
                current.steps = self._steps_of(current_id)
            pending = [self._run(q) for q in pending_ids]
            for q in done_ids:
                if q not in self._finished:
                    self._finished[q] = self._run(q)
            # La caché solo guarda lo que se enseña: no crece con los días.
            self._finished = {q: self._finished[q] for q in done_ids}
            self._steps = {k: v for k, v in self._steps.items() if k[0] == current_id}
            return MissionsView(current, pending, [self._finished[q] for q in done_ids], offset)
