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

from fm.adapters.base import ActionInfo, Job, JobStatus, Telemetry
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
        if now < self.started:
            return "WAITING"          # en cola detrás de otro job
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
        self._paused_since: float | None = None
        self._paused_total = 0.0
        self._last_tick = self._now()     # en tiempo de trabajo, ver _now()

    # ------------------------------------------------------------- conexión
    def connect(self) -> bool:
        return True

    def poll(self) -> Telemetry | None:
        now = self._now()
        current = self._current(now)
        # Batería: por cada job, el tiempo que ha estado en marcha dentro de
        # [último tick, ahora] descarga (o carga, si es un job de carga).
        rate = self.cfg.drain_pct_per_s
        for j in self._jobs.values():
            overlap = min(now, j.ends) - max(self._last_tick, j.started)
            if overlap > 0:
                self.battery += rate * overlap if j.charge else -rate * overlap
        self.battery = min(100.0, max(0.0, self.battery))
        self._last_tick = now
        return Telemetry(
            battery=round(self.battery, 2),
            pose=self.cfg.pose,
            map_id=self.cfg.map_id if self.cfg.pose else None,
            driving=current is not None and not current.charge and self._paused_since is None,
            paused=self._paused_since is not None,
            charging=current is not None and current.charge,
            available=True,
            foreign_busy=False,      # nadie lanza nada "desde la web" en un sim
        )

    def pause(self) -> None:
        """Congela el reloj de los jobs (`_now()` no avanza en pausa)."""
        if self._paused_since is None:
            self._paused_since = self.clock()

    def resume(self) -> None:
        if self._paused_since is not None:
            self._paused_total += self.clock() - self._paused_since
            self._paused_since = None

    def _now(self) -> float:
        """Tiempo "de trabajo": el reloj descontando las pausas."""
        t = self.clock() - self._paused_total
        if self._paused_since is not None:
            t -= self.clock() - self._paused_since
        return t

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
        """Cola secuencial como la de un robot real: el job arranca cuando
        termina el anterior vivo (`priority` se ignora: un solo job en cola
        a la vez es lo habitual en el FM)."""
        now = self._now()
        job_id = str(self._next_id)
        self._next_id += 1
        payload = job.payload or {}
        start = max([now, *(j.ends for j in self._jobs.values() if j.status(now) in ("WAITING", "RUNNING"))])
        self._jobs[job_id] = _SimJob(
            job, start, start + float(payload.get("duration_s", self.cfg.duration_s)),
            fail=job.action.actionType in self.cfg.fail_actions,
            charge=bool(payload.get("charge", False)))
        log.info("[%s] job %s (%s) %s, termina en %.1fs", self.serial, job_id, job.label,
                 "arranca" if start == now else f"en cola (arranca en {start - now:.1f}s)",
                 self._jobs[job_id].ends - now)
        return job_id

    def job_status(self, job_id: str) -> JobStatus | None:
        j = self._jobs.get(job_id)
        return j.status(self._now()) if j else None

    def cancel(self, job_id: str) -> None:
        j = self._jobs.get(job_id)
        if j is not None and j.status(self._now()) in ("WAITING", "RUNNING"):
            j.started = j.ends = self._now()
            j.fail = True

    def charge_job(self) -> Job | None:
        action = Action("charge", "auto-charge", actionDescription="auto-carga del FM")
        return Job(action, "sim 'charge'", {"duration_s": self.cfg.charge_duration_s, "charge": True})

    def extra_state(self, s: State) -> None:
        """Nada que añadir."""

    def describe_actions(self) -> list[ActionInfo]:
        return [ActionInfo(t, [], f"sim {self.cfg.duration_s:.0f}s") for t in sorted(self.cfg.action_types)]
