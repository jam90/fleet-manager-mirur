"""Vuelta a la espera (decisión 136): un robot parado no debe estorbar.

Algunas missions acaban con el robot en un sitio de paso (delante de una
mesa, en un pasillo) y ahí molesta al otro robot. Si un robot lleva
`after_s` segundos parado y sin trabajo, el FM le manda la action
`idle_return.action` (p. ej. `ir_a_espera`). La espera da tiempo a que
llegue una segunda order que siga a la primera; si no llega, libera el
sitio. Genérico, como la auto-carga (`charge.py`): la action se traduce con
el driver del robot igual que la de una order.

Reglas:

- **Parado** = disponible (ni manual, ni error, ni parada de emergencia),
  en modo automático, sin pausa, en el site de la flota y sin nada vivo:
  ni order, ni carga, ni una mission lanzada desde la web. Si deja de
  cumplirse, la cuenta vuelve a empezar.
- **Fuera de la zona** (decisión 138, opcional): con `zone`, un robot que ya
  está dentro de esa zona del mapa (la de espera) no cuenta ni va: ya está
  donde lo mandaría la action. Si el driver no sabe decirlo (zona que no está
  en el mapa, sin pose), se hace como sin zona.
- **Una vez:** tras lanzarla, no se repite hasta que el robot haga otra
  cosa (una order, una carga, una mission de la web). Si no, al llegar a
  la espera volvería a estar parado y se mandaría en bucle. Al arrancar el
  FM cuenta como "acaba de hacer algo": un robot parado se va a la espera.
- **No ocupa al robot:** el asignador lo puede elegir mientras va a la
  espera, y al lanzar una order el FM cancela antes esta mission
  (`Robot.execute`). La auto-carga sí espera detrás (no aborta nada).
- Si falla (la action no se puede traducir, el robot la rechaza o acaba
  FAILED), se dice en el log y no se insiste hasta la próxima vez.
"""
from __future__ import annotations

import itertools
import logging
import math
import time
from dataclasses import dataclass
from typing import Callable

from fm.adapters.base import JOB_ALIVE, Job, JobStatus, RobotDriver, Telemetry, in_zone, job_result
from fm.config import IdleReturnConfig
from fm.vda5050.order import Action, ActionParameter
from fm.vda5050.state import I_IDLE_RETURN, I_IDLE_RETURN_IN, Info
from fm.vda5050.state_builder import StateOverlay

log = logging.getLogger("fm.idle")
_ids = itertools.count(1)


@dataclass
class IdleGuard:
    """Estado de la vuelta a la espera de UN robot."""
    serial: str
    cfg: IdleReturnConfig
    clock: Callable[[], float] = time.monotonic
    armed: bool = True                 # ha hecho algo desde la última vuelta (o acaba de arrancar el FM)
    idle_since: float | None = None    # desde cuándo cumple "parado"
    job: Job | None = None
    job_id: str | None = None
    status: JobStatus = "WAITING"

    @property
    def active(self) -> bool:
        """La mission de espera está posteada y viva."""
        return self.job_id is not None and self.status in JOB_ALIVE

    def remaining(self) -> float | None:
        """Segundos que faltan para lanzarla (None = no está contando)."""
        if self.idle_since is None or not self.cfg.enabled:
            return None
        return max(0.0, self.cfg.after_s - (self.clock() - self.idle_since))

    # ------------------------------------------------------------------ tick
    def tick(self, driver: RobotDriver, busy: bool, ready: bool, telemetry: Telemetry | None = None) -> None:
        """Cada tick tras `driver.poll()`. `busy`: tiene algo vivo que no es
        esta mission (order, carga, mission de la web); `ready`: disponible,
        en automático, sin pausa y en el site de la flota; `telemetry`, para
        saber si ya está en la zona de espera. Nunca lanza."""
        if self.active:
            self._follow(driver)
            return
        if busy:
            self.armed, self.idle_since = True, None
            return
        if not (self.cfg.enabled and self.armed and ready):
            self.idle_since = None
            return
        if self.cfg.zone and in_zone(driver, self.cfg.zone, telemetry):
            self.idle_since = None       # ya está en la espera: ni cuenta ni va
            return
        now = self.clock()
        if self.idle_since is None:
            self.idle_since = now
            return
        if now - self.idle_since < self.cfg.after_s:
            return
        self.armed, self.idle_since = False, None
        action = Action(actionType=self.cfg.action, actionId=f"idle-{self.serial}-{next(_ids)}",
                        actionParameters=[ActionParameter(k, v) for k, v in self.cfg.params.items()])
        try:
            job = driver.translate(action)
            job_id = driver.execute(job, 0)
        except Exception as e:           # OrderRejected (mapa, parámetro…), red, 4xx del robot
            log.warning("[%s] parado %.0fs, pero no se pudo mandar a la espera (%s): %s",
                        self.serial, self.cfg.after_s, self.cfg.action, getattr(e, "description", e))
            return
        self.job, self.job_id, self.status = job, job_id, "WAITING"
        log.info("[%s] parado %.0fs: a la espera con %s job=%s", self.serial, self.cfg.after_s, job.label, job_id)

    def _follow(self, driver: RobotDriver) -> None:
        status = driver.job_status(self.job_id)
        if status is None or status == self.status:
            return
        log.info("[%s] vuelta a la espera job=%s %s → %s", self.serial, self.job_id, self.status, status)
        self.status = status
        if status == "FAILED":
            why = job_result(driver, self.job_id)
            log.warning("[%s] la vuelta a la espera terminó FAILED%s", self.serial, f" ({why})" if why else "")

    def cancel(self, driver: RobotDriver) -> None:
        """Llega una order: fuera la mission de espera. Si no se puede
        cancelar (red), la order se encola detrás en el robot."""
        if not self.active:
            return
        try:
            driver.cancel(self.job_id)
            log.info("[%s] llega una order: se cancela la vuelta a la espera job=%s", self.serial, self.job_id)
        except Exception as e:
            log.warning("[%s] no se pudo cancelar la vuelta a la espera job=%s (%s): la order irá detrás",
                        self.serial, self.job_id, e)
        self.status = "FAILED"

    # ---------------------------------------------------------------- salida
    def decorate(self, ov: StateOverlay) -> StateOverlay:
        """Va a la espera (`IDLE_RETURN`) o cuánto le falta para ir
        (`IDLE_RETURN_IN`, segundos enteros): la web lo enseña como contador."""
        if self.active:
            ov.information.append(Info(I_IDLE_RETURN, "INFO",
                                       f"a la espera: {self.job.label if self.job else self.cfg.action} ({self.status})"))
        elif (left := self.remaining()) is not None:
            ov.information.append(Info(I_IDLE_RETURN_IN, "INFO", str(math.ceil(left))))
        return ov
