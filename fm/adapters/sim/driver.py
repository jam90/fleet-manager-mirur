"""`SimDriver`: implementa `RobotDriver` sin red ni hardware.

```yaml
drivers:
  sim:
    duration_s: 5          # lo que tarda cualquier job (default 5)
    drain_pct_per_s: 0.5   # batería que gasta por segundo mientras ejecuta
robots:
  sim-1:
    driver: sim
    battery: 90            # batería inicial
    pose: [1.0, 2.0, 0.0]  # opcional; sin pose no se publica mobileRobotPosition
    fail_actions: [dejar]  # estos actionTypes terminan FAILED
    actions:               # qué actionTypes acepta (el valor se ignora)
      coger: {}
      dejar: {}
```

El tiempo se inyecta (`clock`) para que los tests no duerman.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping

from fm.adapters.base import Job, JobStatus, Telemetry
from fm.config import ConfigError
from fm.vda5050.order import Action, OrderRejected
from fm.vda5050.state import E_INVALID_ORDER_ACTION, State

log = logging.getLogger("fm.sim")


@dataclass
class SimRobotConfig:
    serial: str
    action_types: set[str]
    battery: float = 100.0
    pose: tuple[float, float, float] | None = None
    map_id: str = "sim-map"
    duration_s: float = 5.0
    drain_pct_per_s: float = 0.0
    fail_actions: set[str] = field(default_factory=set)
    charge_duration_s: float = 30.0


def parse_config(serial: str, raw: Mapping, defaults: Mapping) -> SimRobotConfig:
    m = {**defaults, **raw}
    pose = m.get("pose")
    if pose is not None and len(pose) != 3:
        raise ConfigError(f"[{serial}] 'pose' debe ser [x, y, theta]")
    return SimRobotConfig(
        serial=serial,
        action_types=set(m.get("actions") or {}),
        battery=float(m.get("battery", 100)),
        pose=tuple(float(v) for v in pose) if pose is not None else None,
        map_id=str(m.get("map_id", "sim-map")),
        duration_s=float(m.get("duration_s", 5)),
        drain_pct_per_s=float(m.get("drain_pct_per_s", 0)),
        fail_actions=set(m.get("fail_actions") or []),
        charge_duration_s=float(m.get("charge_duration_s", 30)),
    )


@dataclass
class _SimJob:
    job: Job
    started: float
    ends: float
    fail: bool
    charge: bool = False

    def status(self, now: float) -> JobStatus:
        if now < self.ends:
            return "RUNNING"
        return "FAILED" if self.fail else "FINISHED"


class SimDriver:
    manufacturer = "SIM"

    def __init__(self, cfg: SimRobotConfig, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.serial = cfg.serial
        self.clock = clock
        self.last_error: str | None = None
        self.battery = cfg.battery
        self._jobs: dict[str, _SimJob] = {}
        self._next_id = 1
        self._last_tick = clock()

    # ------------------------------------------------------------- conexión
    def connect(self) -> bool:
        return True

    def poll(self) -> Telemetry | None:
        now = self.clock()
        current = self._current(now)
        # Descarga mientras ejecuta; carga mientras el job es de carga.
        dt = max(0.0, now - self._last_tick)
        self._last_tick = now
        if current is not None:
            rate = self.cfg.drain_pct_per_s
            self.battery += rate * dt if current.charge else -rate * dt
            self.battery = min(100.0, max(0.0, self.battery))
        return Telemetry(
            battery=round(self.battery, 2),
            pose=self.cfg.pose,
            map_id=self.cfg.map_id if self.cfg.pose else None,
            driving=current is not None and not current.charge,
            charging=current is not None and current.charge,
            available=True,
            foreign_busy=False,      # nadie lanza nada "desde la web" en un sim
        )

    def _current(self, now: float) -> _SimJob | None:
        for j in self._jobs.values():
            if j.status(now) == "RUNNING":
                return j
        return None

    # ----------------------------------------------------------------- jobs
    def translate(self, action: Action) -> Job:
        if action.actionType not in self.cfg.action_types:
            raise OrderRejected(E_INVALID_ORDER_ACTION,
                                f"[{self.serial}] actionType '{action.actionType}' no soportado; "
                                f"soporta {sorted(self.cfg.action_types)}")
        return Job(action, f"sim '{action.actionType}'", {"duration_s": self.cfg.duration_s})

    def execute(self, job: Job, priority: int = 0) -> str:
        now = self.clock()
        job_id = str(self._next_id)
        self._next_id += 1
        payload = job.payload or {}
        self._jobs[job_id] = _SimJob(
            job, now, now + float(payload.get("duration_s", self.cfg.duration_s)),
            fail=job.action.actionType in self.cfg.fail_actions,
            charge=bool(payload.get("charge", False)))
        log.info("[%s] job %s (%s) arranca, termina en %.1fs", self.serial, job_id, job.label,
                 self._jobs[job_id].ends - now)
        return job_id

    def job_status(self, job_id: str) -> JobStatus | None:
        j = self._jobs.get(job_id)
        return j.status(self.clock()) if j else None

    def cancel(self, job_id: str) -> None:
        j = self._jobs.get(job_id)
        if j is not None and j.status(self.clock()) == "RUNNING":
            j.ends = self.clock()
            j.fail = True

    def charge_job(self) -> Job | None:
        action = Action("charge", "auto-charge", actionDescription="auto-carga del FM")
        return Job(action, "sim 'charge'", {"duration_s": self.cfg.charge_duration_s, "charge": True})

    def extra_state(self, s: State) -> None:
        """Nada que añadir."""
