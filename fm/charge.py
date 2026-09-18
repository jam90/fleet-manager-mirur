"""Auto-carga (H4): suelo de batería por robot, evaluado en cada tick.

Independiente del bus MQTT y de la marca: el core solo pide al driver
`charge_job()` y lo ejecuta/sigue como cualquier otro job. Reglas cerradas en
CLAUDE.md §7:

- No aborta nada: si el robot ejecuta una order, la carga se encola detrás
  (con `priority`) y arranca al terminar. `battery_min` del asignador evita
  que le entren orders nuevas mientras tanto.
- La carga del FM cuenta como `busy` para el asignador mientras viva.
- Si la carga termina FAILED (abortada desde la web, dock ocupado, ...), se
  espera `abort_cooldown_s` antes de volver a postear, para no entrar en bucle.
- Mientras hay carga activa, `state.information[]` lleva `AUTO_CHARGE` y
  `powerSupply.charging = True` cuando el job está RUNNING.

La mission de carga de los MiR (2026-09-18: docking → charging hasta 70 % →
relative_move) termina `Done` sola; ver "trampa del deadlock" en CLAUDE.md §7.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from fm.adapters.base import Job, JobStatus, RobotDriver, Telemetry
from fm.config import AutoChargeConfig
from fm.vda5050.state import Info
from fm.vda5050.state_builder import StateOverlay

log = logging.getLogger("fm.charge")

JOB_ALIVE = ("WAITING", "RUNNING")


@dataclass
class ChargeGuard:
    """Estado de la auto-carga de UN robot."""
    serial: str
    cfg: AutoChargeConfig
    clock: Callable[[], float] = time.monotonic
    job: Job | None = None
    job_id: str | None = None
    status: JobStatus = "WAITING"
    cooldown_until: float = 0.0
    unsupported: bool = False          # el driver devolvió None en charge_job(): no insistir

    # ------------------------------------------------------------ consultas
    @property
    def active(self) -> bool:
        """Carga del FM posteada y viva (cuenta como busy)."""
        return self.job_id is not None and self.status in JOB_ALIVE

    @property
    def charging(self) -> bool:
        """El job de carga está en ejecución (aprox.: incluye el docking)."""
        return self.job_id is not None and self.status == "RUNNING"

    # ------------------------------------------------------------------ tick
    def tick(self, driver: RobotDriver, telemetry: Telemetry | None) -> None:
        """Llamar cada tick tras `driver.poll()`. Sigue la carga activa y, si
        procede, postea una nueva. Nunca lanza."""
        if self.active:
            self._follow(driver)
            return
        if telemetry is None or not telemetry.available or self.unsupported:
            return
        if telemetry.battery >= self.cfg.battery_floor:
            return
        now = self.clock()
        if now < self.cooldown_until:
            return
        job = driver.charge_job()
        if job is None:
            log.info("[%s] el driver no soporta auto-carga: desactivada", self.serial)
            self.unsupported = True
            return
        try:
            self.job_id = driver.execute(job, self.cfg.priority)
        except Exception as e:
            # Red o rechazo del robot: reintentar tras el cooldown, no cada tick.
            self.cooldown_until = now + self.cfg.abort_cooldown_s
            log.warning("[%s] no se pudo postear la carga (%s); reintento en %.0fs",
                        self.serial, e, self.cfg.abort_cooldown_s)
            return
        self.job, self.status = job, "WAITING"
        log.info("[%s] batería %.1f%% < suelo %.0f%%: auto-carga %s job=%s (prioridad %d)",
                 self.serial, telemetry.battery, self.cfg.battery_floor, job.label,
                 self.job_id, self.cfg.priority)

    def _follow(self, driver: RobotDriver) -> None:
        status = driver.job_status(self.job_id)
        if status is None or status == self.status:
            return
        log.info("[%s] auto-carga job=%s %s → %s", self.serial, self.job_id, self.status, status)
        self.status = status
        if status == "FAILED":
            self.cooldown_until = self.clock() + self.cfg.abort_cooldown_s
            log.warning("[%s] la carga terminó FAILED: no se re-postea hasta dentro de %.0fs",
                        self.serial, self.cfg.abort_cooldown_s)

    # ---------------------------------------------------------------- salida
    def decorate(self, ov: StateOverlay) -> StateOverlay:
        """Refleja la carga activa en el `state` (§7)."""
        if self.active:
            ov.information.append(Info("AUTO_CHARGE", "INFO",
                                       f"{self.job.label if self.job else 'carga'} ({self.status})"))
            if self.charging:
                ov.charging = True
        return ov
