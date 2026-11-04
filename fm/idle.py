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
- **Una parada de seguridad no corta la cuenta** (decisión 172): alguien
  que se acerca demasiado para el robot, pero el robot sigue sin trabajo.
  Con mucho paso de gente, si cada parada pusiera la cuenta a cero, no
  volvería nunca a la espera. Durante la parada se sigue contando; si se
  cumple `after_s`, sale en cuanto se suelte.
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
- Si no se puede mandar (la action no se traduce, el robot la rechaza), se
  dice en el log y no se insiste hasta la próxima vez.
- **Si acaba FAILED** (decisión 171; p. ej. el MiR aborta porque el camino
  a la espera está bloqueado): vuelve a contar `after_s` y lo reintenta,
  hasta `MAX_ATTEMPTS` intentos seguidos. Desde el primer fallo, el `state`
  lleva un aviso (`IDLE_RETURN_FAILED`, WARNING) con el motivo; al agotar
  los intentos se rinde y el aviso pide confirmar que la espera está libre:
  la instantAction propia `retryIdleReturn` (botón en la web) pone la
  cuenta a cero y lo manda ya. Cualquier otra cosa que haga el robot, o
  llegar a la espera, también la pone a cero.
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
from fm.vda5050.state import E_IDLE_RETURN_FAILED, I_IDLE_RETURN, I_IDLE_RETURN_IN, Error, Info
from fm.vda5050.state_builder import StateOverlay

log = logging.getLogger("fm.idle")
_ids = itertools.count(1)

# Intentos seguidos que acaban FAILED antes de rendirse (decisión 171). Cada
# uno puede costar minutos (el MiR espera hasta 60 s a que se libere el
# camino): tres bastan para un bloqueo pasajero.
MAX_ATTEMPTS = 3


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
    failures: int = 0                  # intentos seguidos que acabaron FAILED (decisión 171)
    failure_why: str = ""              # motivo del último, si el driver lo sabe
    wait_reason: str = ""              # por qué no puede ir ahora (en error, en pausa…), para el aviso

    @property
    def active(self) -> bool:
        """La mission de espera está posteada y viva."""
        return self.job_id is not None and self.status in JOB_ALIVE

    @property
    def gave_up(self) -> bool:
        """Agotó los intentos: espera a que alguien confirme que está libre."""
        return self.failures >= MAX_ATTEMPTS

    def remaining(self) -> float | None:
        """Segundos que faltan para lanzarla (None = no está contando)."""
        if self.idle_since is None or not self.cfg.enabled:
            return None
        return max(0.0, self.cfg.after_s - (self.clock() - self.idle_since))

    # ------------------------------------------------------------------ tick
    def tick(self, driver: RobotDriver, busy: bool, ready: bool, telemetry: Telemetry | None = None,
             hold: bool = False, wait_reason: str = "") -> None:
        """Cada tick tras `driver.poll()`. `busy`: tiene algo vivo que no es
        esta mission (order, carga, mission de la web); `ready`: disponible
        (o en parada de seguridad), en automático, sin pausa y en el site de
        la flota; `telemetry`, para saber si ya está en la zona de espera;
        `hold`: en parada de seguridad, cuenta pero no sale; `wait_reason`:
        por qué no puede ir ahora, para el aviso. Nunca lanza."""
        self.wait_reason = wait_reason
        if self.active:
            self._follow(driver)
            return
        if busy:
            self.armed, self.idle_since = True, None
            self.reset_failures()       # ha hecho otra cosa: se empieza de cero
            return
        if not (self.cfg.enabled and self.armed and ready):
            self.idle_since = None
            return
        if self.cfg.zone and in_zone(driver, self.cfg.zone, telemetry):
            self.idle_since = None       # ya está en la espera: ni cuenta ni va
            self.reset_failures()       # (alguien lo llevó a mano, p. ej.)
            return
        now = self.clock()
        if self.idle_since is None:
            self.idle_since = now
            return
        if now - self.idle_since < self.cfg.after_s:
            return
        if hold:                         # tiempo cumplido, pero en parada: sale al soltarse
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
        if status == "FINISHED":
            self.reset_failures()
        elif status == "FAILED":
            self.failures += 1
            self.failure_why = job_result(driver, self.job_id) or ""
            why = f" ({self.failure_why})" if self.failure_why else ""
            if self.gave_up:
                log.warning("[%s] la vuelta a la espera terminó FAILED%s, %d veces seguidas: no se reintenta "
                            "hasta que alguien confirme que está libre (retryIdleReturn)",
                            self.serial, why, self.failures)
            else:
                # Vuelve a contar `after_s`: un bloqueo pasajero (alguien de
                # paso) se arregla solo.
                self.armed = True
                log.warning("[%s] la vuelta a la espera terminó FAILED%s: intento %d de %d en %.0fs si sigue parado",
                            self.serial, why, self.failures + 1, MAX_ATTEMPTS, self.cfg.after_s)

    def retry(self) -> None:
        """Alguien confirma que la espera está libre (instantAction
        `retryIdleReturn`): cuenta a cero y se manda ya, en el próximo tick,
        si el robot está parado y fuera de la zona."""
        self.reset_failures()
        self.armed = True
        self.idle_since = self.clock() - self.cfg.after_s
        log.info("[%s] confirmado que la espera está libre: se manda ya", self.serial)

    def reset_failures(self) -> None:
        self.failures, self.failure_why = 0, ""

    def cancel(self, driver: RobotDriver, why: str = "llega una order") -> None:
        """Llega una order (o el robot entra en mantenimiento): fuera la
        mission de espera. Si no se puede cancelar (red), la order se encola
        detrás en el robot."""
        if not self.active:
            return
        try:
            driver.cancel(self.job_id)
            log.info("[%s] %s: se cancela la vuelta a la espera job=%s", self.serial, why, self.job_id)
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
        if self.failures and not self.active:
            why = f" ({self.failure_why})" if self.failure_why else ""
            if self.gave_up:
                text = (f"no ha podido ir a la espera{why}, {self.failures} veces seguidas: ¿está bloqueada? "
                        "Cuando esté libre, «reintentar» (retryIdleReturn)")
            elif self.wait_reason:       # p. ej. el MiR queda en error tras abortar: nadie lo reintenta solo
                text = (f"no ha podido ir a la espera{why}. {self.wait_reason[0].upper()}{self.wait_reason[1:]}; "
                        f"después lo reintentará (intento {self.failures + 1} de {MAX_ATTEMPTS})")
            else:
                text = (f"no ha podido ir a la espera{why}: intento {self.failures + 1} de {MAX_ATTEMPTS} "
                        f"cuando lleve {self.cfg.after_s:.0f} s parado")
            ov.errors.append(Error(E_IDLE_RETURN_FAILED, "WARNING", text))
        return ov
