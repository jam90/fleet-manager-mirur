"""Estado en runtime de un robot: driver de la marca + seguimiento de la
order VDA + última telemetría. Un objeto por robot, vivo durante toda la
ejecución del FM. No sabe de qué marca es el robot: todo pasa por `driver`.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from fm.adapters.base import Job, RobotDriver, Telemetry
from fm.assigner import RobotSnapshot
from fm.charge import ChargeGuard
from fm.config import AutoChargeConfig, RobotConfig
from fm.idle import IdleGuard
from fm.maintenance import MaintenanceStore
from fm.orders import OrderTracker, pick_action
from fm.vda5050.order import Order, OrderRejected
from fm.vda5050.state import (E_MOBILE_ROBOT_NOT_AVAILABLE, E_ORDER_EXECUTION_FAILED,
                              E_ROBOT_OUTSIDE_SITE, E_ROBOT_UNREACHABLE, I_MAINTENANCE,
                              I_MAINTENANCE_PENDING, Error, Info, State)
from fm.tasks import iso
from fm.vda5050.state_builder import to_vda_state

log = logging.getLogger("fm.robot")

# Backoff tras fallos de red consecutivos: 1, 2, 4, 8, 8, ... segundos entre
# intentos. Un robot apagado deja de costar un timeout por tick (decisión 42).
BACKOFF_MAX_S = 8.0
# Si el robot responde pero `connect()` falla (p.ej. `mission_group` mal
# escrito), no reintentar en cada tick: serían varias peticiones REST y un
# warning por segundo mientras dure el problema.
CONNECT_RETRY_S = 5.0


class Robot:
    def __init__(self, cfg: RobotConfig, driver: RobotDriver,
                 auto_charge: AutoChargeConfig | None = None,
                 clock: Callable[[], float] = time.monotonic, site: str | None = None):
        self.cfg = cfg
        self.clock = clock
        self.serial = cfg.serial
        self.driver = driver
        # Prefijo [serial] en cada línea para poder filtrar el log por robot.
        self.log = logging.LoggerAdapter(log, {})
        self.log.process = lambda msg, kw: (f"[{self.serial}] {msg}", kw)
        self.connected = False
        self.last_telemetry: Telemetry | None = None
        self.poll_failures = 0           # fallos de red consecutivos
        self.next_poll_at = 0.0          # no volver a preguntar al robot antes de esto
        self.next_connect_at = 0.0       # no reintentar connect() antes de esto
        # Config cambiada en caliente (fm/reload.py): volver a cargar los
        # índices del driver en el próximo poll, que corre en el hilo del robot.
        self.reindex = False
        self.orders = OrderTracker(self.serial)
        self.charge = ChargeGuard(self.serial, auto_charge or AutoChargeConfig())
        self.idle = IdleGuard(self.serial, cfg.idle_return, clock)    # vuelta a la espera (decisión 136)
        # Site de la flota (`FleetConfig.site`, decisión 129); lo cambia en
        # caliente `fm/reload.py`.
        self.site = site
        # Modo mantenimiento (decisiones 167–170): epoch en que se pidió, o
        # None. `maintenance_store` lo guarda en disco (lo pone el
        # `Dispatcher`); sin él, solo vive en RAM.
        self.maintenance_since: float | None = None
        self.maintenance_store: MaintenanceStore | None = None
        self._in_service = False          # para avisar en el log una sola vez al entrar

    @property
    def manufacturer(self) -> str:
        return self.driver.manufacturer

    @property
    def last_error(self) -> str | None:
        return self.driver.last_error

    # ------------------------------------------------------------------ tick
    def connect(self) -> bool:
        """Índices/handshake del driver. Si falla, `poll()` lo reintenta cada
        `CONNECT_RETRY_S` mientras el robot responda."""
        self.connected = self.driver.connect()
        return self.connected

    def poll(self) -> Telemetry | None:
        """Telemetría del driver (None = inalcanzable) y avance del job en
        curso. Solo toca red de ESTE robot: `run_fm` la ejecuta en paralelo
        para todos los robots (un hilo por robot) y luego sigue en el hilo
        principal. Con el robot inalcanzable se aplica backoff: entre
        intentos se conserva `last_telemetry = None` y se publica igual."""
        now = self.clock()
        if now < self.next_poll_at:
            return None
        self.last_telemetry = self.driver.poll()
        if self.last_telemetry is None:
            self.poll_failures += 1
            wait = min(2.0 ** (self.poll_failures - 1), BACKOFF_MAX_S)
            self.next_poll_at = now + wait
            if self.poll_failures >= 2:
                self.log.info("sin respuesta (%d seguidos): próximo intento en %.0fs",
                              self.poll_failures, wait)
            return None
        if self.poll_failures:
            self.log.info("vuelve a responder tras %d fallos", self.poll_failures)
        self.poll_failures = 0
        self.next_poll_at = 0.0
        if (not self.connected or self.reindex) and now >= self.next_connect_at:
            # Se baja la bandera ANTES de conectar: si la config vuelve a
            # cambiar mientras tanto, el siguiente poll reindexa otra vez.
            self.reindex = False
            if not self.connect():       # reintento tras un arranque sin red
                self.next_connect_at = now + CONNECT_RETRY_S
        self.orders.poll(self.driver)
        self.orders.watch_duration(now, self.last_telemetry.paused, self.cfg.expected_s)
        # En mantenimiento no se lanza ninguna carga, pero la que ya estaba
        # en marcha se sigue hasta que acabe (decisión 168).
        self.charge.tick(self.driver, self.last_telemetry, launch=not self.in_maintenance)
        t = self.last_telemetry
        self.idle.tick(self.driver, busy=self.busy,
                       ready=(t.available and t.operating_mode == "AUTOMATIC" and not t.paused
                              and not self.outside_site and not self.in_maintenance), telemetry=t)
        self._log_service()
        return self.last_telemetry

    def overlay(self):
        """Lo que el FM sabe de este robot y el driver no: order, auto-carga,
        vuelta a la espera y mantenimiento."""
        ov = self.idle.decorate(self.charge.decorate(self.orders.overlay()))
        if self.in_service:
            ov.information.append(Info(I_MAINTENANCE, "INFO", iso(self.maintenance_since)))
        elif self.in_maintenance:
            ov.information.append(Info(I_MAINTENANCE_PENDING, "INFO", self.maintenance_reason))
        return ov

    def state(self, header: dict) -> State:
        """`state` VDA de este robot: telemetría + overlay del FM + el gancho
        opcional `extra_state()` del driver (campos que el core no deduce).
        El gancho solo se llama con telemetría fresca: sin ella no hay nada
        nuevo que decir del robot."""
        s = to_vda_state(header, self.last_telemetry, self.overlay(), error=self.last_error)
        if self.in_service and self.last_telemetry is not None:
            # SERVICE (VDA §6.6.6): el gestor de flota no le manda orders ni
            # actions. Manda sobre lo que diga el robot (p. ej. la llave en
            # manual): el FM ha decidido no darle trabajo.
            s.operatingMode = "SERVICE"
        if self.outside_site:
            s.errors.append(Error(E_ROBOT_OUTSIDE_SITE, "WARNING", self.outside_reason))
        extra = getattr(self.driver, "extra_state", None)
        if extra is not None and self.last_telemetry is not None:
            try:
                extra(s)
            except Exception as e:   # un driver con bug no debe parar el tick de toda la flota
                self.log.error("extra_state() lanzó %s: %s", type(e).__name__, e)
        return s

    # ---------------------------------------------------------------- orders
    @property
    def busy(self) -> bool:
        """Ocupado = job del FM vivo (order o auto-carga), o el robot ejecuta
        algo que no es nuestro (lanzado desde la web) — decisión 16. Lo último
        lo decide el driver. Ir a la espera no cuenta: se cancela si llega
        una order (decisión 136)."""
        return self.orders.busy or self.charge.active or self.foreign_busy

    @property
    def foreign_busy(self) -> bool:
        t = self.last_telemetry
        return t is not None and t.foreign_busy

    @property
    def outside_site(self) -> bool:
        """Su mapa activo es de otro site que el de la flota (decisión 129).
        Si la flota no tiene site o el driver no sabe el del robot, no."""
        t = self.last_telemetry
        return bool(self.site and t is not None and t.site_id and t.site_id != self.site)

    @property
    def outside_reason(self) -> str:
        t = self.last_telemetry
        where = f"«{t.site_name}»" if t and t.site_name else (t.site_id if t else "")
        return (f"su mapa activo es del site {where}, no del de la flota: no recibe orders hasta que "
                "se cambie de mapa en su web")

    @property
    def available(self) -> bool:
        """Puede recibir orders: el driver lo da por disponible, está en el
        site de la flota y no está en mantenimiento. La auto-carga no mira el
        site (`charge.py`)."""
        t = self.last_telemetry
        return t is not None and t.available and not self.outside_site and not self.in_maintenance

    def snapshot(self) -> RobotSnapshot:
        t = self.last_telemetry
        charging = self.charge.charging or bool(t and t.charging)
        reason = (self.maintenance_reason if self.in_maintenance
                  else self.outside_reason if self.outside_site else "")
        return RobotSnapshot(self.serial, t.battery if t else 0.0, self.busy,
                             self.available, charging, t.pose[:2] if t and t.pose else None,
                             reason=reason)

    # ---------------------------------------------------------- mantenimiento
    # Decisiones 167–170. Pedirlo saca al robot del reparto en el acto; si
    # tiene una order o una carga del FM en marcha, se deja terminar (VDA
    # pide borrar la order al entrar en SERVICE, y aquí no se aborta nada):
    # mientras tanto está "pendiente" y sigue en AUTOMATIC. Una mission
    # lanzada desde su web no lo retrasa: no es del FM.
    @property
    def in_maintenance(self) -> bool:
        """Se ha pedido el mantenimiento (pendiente o ya en SERVICE)."""
        return self.maintenance_since is not None

    @property
    def in_service(self) -> bool:
        """En mantenimiento de verdad: ya no le queda trabajo del FM."""
        return self.in_maintenance and not (self.orders.busy or self.charge.active)

    @property
    def maintenance_reason(self) -> str:
        if self.in_service:
            return "en mantenimiento: no recibe orders hasta que salga"
        what = "su order" if self.orders.busy else "su carga"
        return f"pasa a mantenimiento al terminar {what}: no recibe orders nuevas"

    def start_maintenance(self, now: float | None = None) -> bool:
        """Pide el mantenimiento. False si ya lo estaba (no cambia la hora)."""
        if self.in_maintenance:
            return False
        self.maintenance_since = time.time() if now is None else now
        if self.maintenance_store is not None:
            self.maintenance_store.set(self.serial, self.maintenance_since)
        # Ir a la espera es cosa del FM y se puede cortar (decisión 136).
        self.idle.cancel(self.driver, why="entra en mantenimiento")
        self.log.info("mantenimiento pedido%s", "" if self.in_service else
                      " (pendiente: " + self.maintenance_reason.split(":")[0] + ")")
        self._log_service()
        return True

    def stop_maintenance(self) -> bool:
        """Sale del mantenimiento. False si no lo estaba."""
        if not self.in_maintenance:
            return False
        self.maintenance_since = None
        self._in_service = False
        if self.maintenance_store is not None:
            self.maintenance_store.set(self.serial, None)
        # Alguien lo ha movido o tocado: que vuelva a la espera si se queda
        # parado fuera de ella, como tras una order.
        self.idle.armed = True
        self.log.info("sale de mantenimiento: vuelve a recibir orders")
        return True

    def _log_service(self) -> None:
        if self.in_service and not self._in_service:
            self.log.info("en mantenimiento (SERVICE): el FM no le manda nada")
        self._in_service = self.in_service

    def translate_order(self, order: Order) -> Job:
        """Valida reglas de ciclo de vida + traduce a `Job` del driver. Sin red.
        Lanza IgnoreOrder / OrderRejected."""
        self.orders.check_new(order, self.last_telemetry)
        if self.in_maintenance:
            raise OrderRejected(E_MOBILE_ROBOT_NOT_AVAILABLE, self.maintenance_reason)
        if self.outside_site:
            raise OrderRejected(E_MOBILE_ROBOT_NOT_AVAILABLE, self.outside_reason)
        if self.foreign_busy:
            raise OrderRejected(E_MOBILE_ROBOT_NOT_AVAILABLE,
                                "el robot ejecuta un trabajo ajeno al FM (lanzado desde la web)")
        action = pick_action(order, self.orders.done_action_ids(order))
        return self.driver.translate(action)

    def execute(self, order: Order, job: Job, priority: int = 0) -> str:
        """Lanza el job en el robot y lo registra en el tracker. Devuelve el job_id.

        Si el robot no lo acepta (red caída, 4xx del MiR...) lanza
        `OrderRejected`, como cualquier otro rechazo: así `fleet/order` recibe
        su `order_response REJECTED` y `<serial>/order` deja el error en
        `state.errors`, en vez de acabar en una excepción sin respuesta."""
        self.idle.cancel(self.driver)        # si iba a la espera, deja de ir
        try:
            job_id = self.driver.execute(job, priority)
        except OrderRejected:
            raise
        except Exception as e:
            # OSError = no se llegó al robot (las excepciones de red de
            # `requests` heredan de él); lo demás, el robot dijo que no.
            # Ojo: tras un timeout de lectura el robot pudo encolarla igual;
            # entonces se verá como trabajo ajeno (`foreign_busy`) y el FM no
            # le mandará nada más hasta que termine.
            error_type = E_ROBOT_UNREACHABLE if isinstance(e, OSError) else E_ORDER_EXECUTION_FAILED
            raise OrderRejected(error_type, f"el robot no aceptó {job.label}: "
                                            f"{type(e).__name__}: {str(e)[:160]}") from e
        self.orders.accept(order, job, job_id)
        return job_id
