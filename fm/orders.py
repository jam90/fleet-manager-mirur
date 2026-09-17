"""Estado de la order VDA de un robot y su seguimiento a través del driver.

Un `OrderTracker` por robot. Sabe qué order está activa, qué `Job` del driver
la ejecuta, y traduce el estado de ese job a `actionStates[]`. También guarda
los errores de rechazo que la norma manda reportar "hasta que se acepte una
nueva order" (decisión 2). No toca la red: pregunta `driver.job_status()`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fm.adapters.base import Job, JobStatus, RobotDriver, Telemetry
from fm.vda5050.order import Action, Order, OrderRejected
from fm.vda5050.state import (E_MOBILE_ROBOT_NOT_AVAILABLE, E_ORDER_EXECUTION_FAILED,
                              E_OTHER_ORDER_ACTIVE, E_OUTDATED_ORDER_UPDATE, E_VALIDATION_FAILURE,
                              ActionState, Error, error_for_order)
from fm.vda5050.state_builder import StateOverlay

log = logging.getLogger("fm.orders")

JOB_ALIVE = ("WAITING", "RUNNING")


class IgnoreOrder(Exception):
    """Order repetida (mismo orderId/orderUpdateId): se descarta en silencio."""


def pick_action(order: Order, done_action_ids: set[str] = frozenset()) -> Action:
    """La única action a ejecutar (decisión 10/11): entre las de los nodos
    released, la primera cuyo actionId no esté ya FINISHED. Más de una
    pendiente → VALIDATION_FAILURE (mejor rechazar que ejecutar a medias).
    Regla VDA del FM, independiente de la marca."""
    pending = [a for a in order.released_actions() if a.actionId not in done_action_ids]
    if not pending:
        raise OrderRejected(E_VALIDATION_FAILURE, "la order no contiene ninguna action que ejecutar")
    if len(pending) > 1:
        raise OrderRejected(E_VALIDATION_FAILURE,
                            f"solo se admite una action por order; llegan {len(pending)}: "
                            + ", ".join(f"{a.actionType}/{a.actionId}" for a in pending))
    return pending[0]


@dataclass
class ActiveOrder:
    order: Order
    job: Job
    job_id: str
    job_status: JobStatus = "WAITING"     # = actionStatus VDA de la action ejecutada
    finished_action_ids: set[str] = field(default_factory=set)

    @property
    def alive(self) -> bool:
        return self.job_status in JOB_ALIVE


class OrderTracker:
    def __init__(self, serial: str):
        self.serial = serial
        self.active: ActiveOrder | None = None
        self.order_errors: list[Error] = []       # rechazos de order; se vacían al aceptar otra
        self.exec_errors: list[Error] = []        # fallo de ejecución de la order activa
        self.instant_errors: list[Error] = []     # (H5) rechazos de instantActions
        self.instant_states: list[ActionState] = []

    # ------------------------------------------------------------ consultas
    @property
    def busy(self) -> bool:
        return self.active is not None and self.active.alive

    def check_new(self, order: Order, telemetry: Telemetry | None) -> None:
        """Reglas §6.1.4 sobre `orderId`/`orderUpdateId` y disponibilidad del
        robot. Lanza `IgnoreOrder` o `OrderRejected`; si no lanza, se puede
        traducir y postear."""
        if telemetry is None:
            raise OrderRejected(E_MOBILE_ROBOT_NOT_AVAILABLE, "sin telemetría del robot")
        if not telemetry.available:
            raise OrderRejected(E_MOBILE_ROBOT_NOT_AVAILABLE,
                                telemetry.unavailable_reason or "robot no disponible")
        a = self.active
        if a is None:
            return
        same = a.order.orderId == order.orderId
        if same and order.orderUpdateId == a.order.orderUpdateId:
            raise IgnoreOrder()
        if same and order.orderUpdateId < a.order.orderUpdateId:
            raise OrderRejected(E_OUTDATED_ORDER_UPDATE,
                                f"orderUpdateId {order.orderUpdateId} < activo {a.order.orderUpdateId}")
        if a.alive:
            if same:
                raise OrderRejected(E_VALIDATION_FAILURE,
                                    "v1 no admite ampliar una order en curso (orderUpdateId > activo)")
            raise OrderRejected(E_OTHER_ORDER_ACTIVE,
                                f"order '{a.order.orderId}' aún activa ({a.job_status})")

    def done_action_ids(self, order: Order) -> set[str]:
        """Para un update de una order terminada: actions ya FINISHED."""
        a = self.active
        if a and a.order.orderId == order.orderId:
            return set(a.finished_action_ids)
        return set()

    # -------------------------------------------------------------- eventos
    def accept(self, order: Order, job: Job, job_id: str) -> None:
        prev_done = self.done_action_ids(order)
        self.active = ActiveOrder(order, job, job_id, finished_action_ids=prev_done)
        self.order_errors.clear()
        self.exec_errors.clear()
        log.info("[%s] order %s/%d aceptada → %s job=%s", self.serial, order.orderId,
                 order.orderUpdateId, job.label, job_id)

    def reject(self, order_id: str | None, err: OrderRejected) -> Error:
        e = error_for_order(err.error_type, err.description, order_id, level="WARNING")
        self.order_errors.append(e)
        log.warning("[%s] order %s rechazada: %s", self.serial, order_id, err)
        return e

    def poll(self, driver: RobotDriver) -> None:
        """Cada tick: refresca el estado del job por el driver (None = sin
        respuesta; se conserva el último estado y se reintenta)."""
        a = self.active
        if a is None or not a.alive:
            return
        status = driver.job_status(a.job_id)
        if status is None or status == a.job_status:
            return
        log.info("[%s] order %s: %s job=%s %s → %s", self.serial, a.order.orderId,
                 a.job.label, a.job_id, a.job_status, status)
        a.job_status = status
        if status == "FINISHED":
            a.finished_action_ids.add(a.job.action.actionId)
        elif status == "FAILED" and not self.exec_errors:
            self.exec_errors.append(error_for_order(
                E_ORDER_EXECUTION_FAILED, f"{a.job.label} terminó FAILED",
                a.order.orderId, level="WARNING", action_id=a.job.action.actionId))

    # -------------------------------------------------------------- salida
    def overlay(self) -> StateOverlay:
        ov = StateOverlay()
        a = self.active
        if a is not None:
            ov.order_id = a.order.orderId
            ov.order_update_id = a.order.orderUpdateId
            # Todas las actions de la order: la ejecutada con su fase, las
            # terminadas en updates anteriores FINISHED.
            for act in a.order.released_actions():
                if act.actionId == a.job.action.actionId:
                    st = a.job_status
                else:
                    st = "FINISHED" if act.actionId in a.finished_action_ids else "WAITING"
                ov.action_states.append(ActionState(act.actionId, st, act.actionType))
        ov.errors = [*self.order_errors, *self.exec_errors, *self.instant_errors]
        ov.instant_action_states = list(self.instant_states)
        return ov
