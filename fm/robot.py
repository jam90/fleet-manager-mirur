"""Estado en runtime de un robot: driver de la marca + seguimiento de la
order VDA + última telemetría. Un objeto por robot, vivo durante toda la
ejecución del FM. No sabe de qué marca es el robot: todo pasa por `driver`.
"""
from __future__ import annotations

import logging

from fm.adapters.base import Job, RobotDriver, Telemetry
from fm.assigner import RobotSnapshot
from fm.charge import ChargeGuard
from fm.config import AutoChargeConfig, RobotConfig
from fm.orders import OrderTracker, pick_action
from fm.vda5050.order import Order, OrderRejected
from fm.vda5050.state import E_MOBILE_ROBOT_NOT_AVAILABLE

log = logging.getLogger("fm.robot")


class Robot:
    def __init__(self, cfg: RobotConfig, driver: RobotDriver,
                 auto_charge: AutoChargeConfig | None = None):
        self.cfg = cfg
        self.serial = cfg.serial
        self.driver = driver
        # Prefijo [serial] en cada línea para poder filtrar el log por robot.
        self.log = logging.LoggerAdapter(log, {})
        self.log.process = lambda msg, kw: (f"[{self.serial}] {msg}", kw)
        self.connected = False
        self.last_telemetry: Telemetry | None = None
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
        """Índices/handshake del driver. Se reintenta cada tick hasta que vaya."""
        self.connected = self.driver.connect()
        return self.connected

    def poll(self) -> Telemetry | None:
        """Telemetría del driver (None = inalcanzable) y avance del job en curso."""
        self.last_telemetry = self.driver.poll()
        if self.last_telemetry is not None and not self.connected:
            self.connect()   # reintento tras un arranque sin red
        self.orders.poll(self.driver)
        self.charge.tick(self.driver, self.last_telemetry)
        return self.last_telemetry

    def overlay(self):
        """Lo que el FM sabe de este robot y el driver no: order + auto-carga."""
        return self.charge.decorate(self.orders.overlay())

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
        """Lanza el job en el robot y lo registra en el tracker. Devuelve el job_id."""
        job_id = self.driver.execute(job, priority)
        self.orders.accept(order, job, job_id)
        return job_id
