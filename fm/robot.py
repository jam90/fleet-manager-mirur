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
from fm.orders import OrderTracker, pick_action
from fm.vda5050.order import Order, OrderRejected
from fm.vda5050.state import (E_MOBILE_ROBOT_NOT_AVAILABLE, E_ORDER_EXECUTION_FAILED,
                              E_ROBOT_UNREACHABLE, State)
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
                 clock: Callable[[], float] = time.monotonic):
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
        self.charge.tick(self.driver, self.last_telemetry)
        return self.last_telemetry

    def overlay(self):
        """Lo que el FM sabe de este robot y el driver no: order + auto-carga."""
        return self.charge.decorate(self.orders.overlay())

    def state(self, header: dict) -> State:
        """`state` VDA de este robot: telemetría + overlay del FM + el gancho
        opcional `extra_state()` del driver (campos que el core no deduce).
        El gancho solo se llama con telemetría fresca: sin ella no hay nada
        nuevo que decir del robot."""
        s = to_vda_state(header, self.last_telemetry, self.overlay(), error=self.last_error)
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
        lo decide el driver."""
        return self.orders.busy or self.charge.active or self.foreign_busy

    @property
    def foreign_busy(self) -> bool:
        t = self.last_telemetry
        return t is not None and t.foreign_busy

    @property
    def available(self) -> bool:
        t = self.last_telemetry
        return t is not None and t.available

    def snapshot(self) -> RobotSnapshot:
        t = self.last_telemetry
        charging = self.charge.charging or bool(t and t.charging)
        return RobotSnapshot(self.serial, t.battery if t else 0.0, self.busy,
                             self.available, charging, t.pose[:2] if t and t.pose else None)

    def translate_order(self, order: Order) -> Job:
        """Valida reglas de ciclo de vida + traduce a `Job` del driver. Sin red.
        Lanza IgnoreOrder / OrderRejected."""
        self.orders.check_new(order, self.last_telemetry)
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
