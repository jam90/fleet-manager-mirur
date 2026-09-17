"""`MirDriver`: implementa `RobotDriver` (fm/adapters/base.py) sobre `MirClient`.

Aquí vive todo lo que el core no debe saber del MiR: los índices nombre →
GUID (distintos en cada robot, §9.2), la `mission_queue` y sus estados, y
qué missions lanzó el FM (para distinguirlas de las lanzadas desde la web).
La traducción pura está en `translate.py`; aquí solo la parte con red.
"""
from __future__ import annotations

import logging

from fm.adapters.base import Job, JobStatus, Telemetry
from fm.adapters.mir.client import MirClient, MirStatus
from fm.adapters.mir.config import MirRobotConfig
from fm.adapters.mir.translate import MissionRequest, from_vda_order, to_telemetry
from fm.vda5050.order import Action, OrderRejected
from fm.vda5050.state import E_MOBILE_ROBOT_NOT_AVAILABLE, State

log = logging.getLogger("fm.mir")

# mission_queue.state → JobStatus (= actionStatus VDA, granularidad gruesa, §5.2)
QUEUE_TO_JOB: dict[str, JobStatus] = {
    "Pending": "WAITING", "Executing": "RUNNING", "Done": "FINISHED",
    "Aborted": "FAILED", "Cancelled": "FAILED", "Canceled": "FAILED",
}


class MirDriver:
    manufacturer = "MiR"

    def __init__(self, cfg: MirRobotConfig, client: MirClient | None = None):
        self.cfg = cfg
        self.serial = cfg.serial
        self.client = client or MirClient(cfg.host, cfg.auth)
        # Prefijo [serial] en cada línea para poder filtrar el log por robot.
        self.log = logging.LoggerAdapter(log, {})
        self.log.process = lambda msg, kw: (f"[{self.serial}] {msg}", kw)
        # Índices por robot (los GUIDs no coinciden entre robots).
        self.missions: dict[str, str] = {}              # nombre → GUID
        self.positions: dict[str, str] = {}             # nombre → GUID
        self.mission_inputs: dict[str, set[str]] = {}   # GUID mission → input_names
        self.indexed = False
        self.last_status: MirStatus | None = None
        self.last_error: str | None = None
        # Entradas de mission_queue lanzadas por el FM: lo demás es "ajeno".
        self._own_queue_ids: set[int] = set()

    # ------------------------------------------------------------- conexión
    def connect(self) -> bool:
        """Resuelve nombres → GUID. Tolerante: si falla, se reintenta en el
        próximo tick y mientras tanto el robot solo publica telemetría."""
        try:
            self.missions = self.client.index_missions_by_name(self.cfg.mission_group)
            st = self.last_status or self.client.status_get()
            self.positions = self.client.index_positions_by_name(st.map_id)
            wanted = {a.mission for a in self.cfg.actions.values()}
            if self.cfg.charge_mission:
                wanted.add(self.cfg.charge_mission)
            for name in sorted(wanted):
                guid = self.missions.get(name)
                if guid is None:
                    self.log.warning("mission '%s' no existe en este robot (irá a NO_ROUTE_TO_TARGET)", name)
                    continue
                self.mission_inputs[guid] = self.client.index_mission_params(guid)
            self.indexed = True
            self.log.info("índices: %d missions, %d positions", len(self.missions), len(self.positions))
            return True
        except Exception as e:   # red, 4xx, JSON raro: todo se reintenta luego
            self.log.warning("no se pudieron cargar los índices: %s", e)
            return False

    def poll(self) -> Telemetry | None:
        """GET /status tolerante → `Telemetry`. None si falla (motivo en `last_error`)."""
        try:
            self.last_status = self.client.status_get()
            self.last_error = None
        except Exception as e:
            # Resumen corto: el detalle completo de requests es ilegible en state.errors
            self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
            self.log.warning("GET /status falló: %s", self.last_error)
            return None
        return to_telemetry(self.last_status, self._own_queue_ids)

    # ----------------------------------------------------------------- jobs
    def translate(self, action: Action) -> Job:
        """`Action` VDA → `Job` con la `MissionRequest` a postear. Sin red."""
        if not self.indexed:
            raise OrderRejected(E_MOBILE_ROBOT_NOT_AVAILABLE, "índices de missions/positions no cargados")
        req = from_vda_order(action, self.cfg, self.missions, self.positions,
                             self.mission_inputs, self.cfg.positions_allowlist)
        return Job(action, f"mission '{req.mission_name}'", req)

    def execute(self, job: Job, priority: int = 0) -> str:
        req: MissionRequest = job.payload
        q = self.client.mission_queue_post(req.mission_guid, req.parameters or None, priority)
        qid = int(q["id"])
        self._own_queue_ids.add(qid)
        return str(qid)

    def job_status(self, job_id: str) -> JobStatus | None:
        try:
            q = self.client.mission_queue_id_get(int(job_id))
        except Exception as e:
            self.log.warning("GET mission_queue/%s falló: %s", job_id, e)
            return None
        state = str(q.get("state", ""))
        status = QUEUE_TO_JOB.get(state)
        if status is None:
            self.log.warning("mission_queue/%s en estado desconocido '%s'", job_id, state)
        elif status in ("FINISHED", "FAILED"):
            self._own_queue_ids.discard(int(job_id))
        return status

    def cancel(self, job_id: str) -> None:
        self.client.mission_queue_id_delete(int(job_id))

    def charge_job(self) -> Job | None:
        """Job de auto-carga (H4): la mission configurada, sin parámetros."""
        name = self.cfg.charge_mission
        guid = self.missions.get(name) if name else None
        if guid is None:
            return None
        action = Action("charge", "auto-charge", actionDescription="auto-carga del FM")
        return Job(action, f"mission '{name}'", MissionRequest(action, name, guid, []))

    def extra_state(self, s: State) -> None:
        """Nada que añadir: el MiR no expone más de lo que ya va en Telemetry."""
