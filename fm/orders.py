"""Estado de la order VDA de un robot y su seguimiento en `mission_queue`.

Un `OrderTracker` por robot. Sabe qué order está activa, qué entrada de la
cola del MiR la ejecuta, y traduce el estado de esa entrada a
`actionStates[]`. También guarda los errores de rechazo que la norma manda
reportar "hasta que se acepte una nueva order" (decisión 2).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fm.adapters.mir import MissionRequest, StateOverlay
from fm.mir_client import (QUEUE_ALIVE, STATE_EMERGENCY_STOP, STATE_ERROR, STATE_MANUAL,
                           MirClient, MirStatus)
from fm.vda5050.order import Order, OrderRejected
from fm.vda5050.state import (E_MOBILE_ROBOT_NOT_AVAILABLE, E_ORDER_EXECUTION_FAILED,
                              E_OTHER_ORDER_ACTIVE, E_OUTDATED_ORDER_UPDATE, E_VALIDATION_FAILURE,
                              ActionState, Error, Info, error_for_order)

log = logging.getLogger("fm.orders")

# mission_queue.state → actionStatus VDA (granularidad gruesa, §5.2 del brief)
QUEUE_TO_ACTION = {"Pending": "WAITING", "Executing": "RUNNING", "Done": "FINISHED",
                   "Aborted": "FAILED", "Cancelled": "FAILED", "Canceled": "FAILED"}


class IgnoreOrder(Exception):
    """Order repetida (mismo orderId/orderUpdateId): se descarta en silencio."""


@dataclass
class ActiveOrder:
    order: Order
    request: MissionRequest
    queue_id: int
    queue_state: str = "Pending"
    action_status: str = "WAITING"
    finished_action_ids: set[str] = field(default_factory=set)

    @property
    def alive(self) -> bool:
        return self.queue_state in QUEUE_ALIVE


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

    def check_new(self, order: Order, status: MirStatus | None) -> None:
        """Reglas §6.1.4 sobre `orderId`/`orderUpdateId` y disponibilidad del
        robot. Lanza `IgnoreOrder` o `OrderRejected`; si no lanza, se puede
        traducir y postear."""
        if status is None:
            raise OrderRejected(E_MOBILE_ROBOT_NOT_AVAILABLE, "sin telemetría REST del robot")
        if status.state_id in (STATE_MANUAL, STATE_ERROR, STATE_EMERGENCY_STOP):
            raise OrderRejected(E_MOBILE_ROBOT_NOT_AVAILABLE,
                                f"robot en estado {status.state_text} (state_id={status.state_id})")
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
                                f"order '{a.order.orderId}' aún activa ({a.queue_state})")

    def done_action_ids(self, order: Order) -> set[str]:
        """Para un update de una order terminada: actions ya FINISHED."""
        a = self.active
        if a and a.order.orderId == order.orderId:
            return set(a.finished_action_ids)
        return set()

    # -------------------------------------------------------------- eventos
    def accept(self, order: Order, request: MissionRequest, queue_id: int) -> None:
        prev_done = self.done_action_ids(order)
        self.active = ActiveOrder(order, request, queue_id, finished_action_ids=prev_done)
        self.order_errors.clear()
        self.exec_errors.clear()
        log.info("[%s] order %s/%d aceptada → '%s' queue id=%d", self.serial, order.orderId,
                 order.orderUpdateId, request.mission_name, queue_id)

    def reject(self, order_id: str | None, err: OrderRejected) -> Error:
        e = error_for_order(err.error_type, err.description, order_id, level="WARNING")
        self.order_errors.append(e)
        log.warning("[%s] order %s rechazada: %s", self.serial, order_id, err)
        return e

    def poll(self, client: MirClient) -> None:
        """Cada tick: refresca el estado de la entrada de la cola."""
        a = self.active
        if a is None or not a.alive:
            return
        try:
            q = client.mission_queue_id_get(a.queue_id)
        except Exception as e:
            log.warning("[%s] GET mission_queue/%d falló: %s", self.serial, a.queue_id, e)
            return
        state = str(q.get("state", a.queue_state))
        if state != a.queue_state:
            log.info("[%s] order %s: mission_queue %d %s → %s", self.serial, a.order.orderId,
                     a.queue_id, a.queue_state, state)
        a.queue_state = state
        a.action_status = QUEUE_TO_ACTION.get(state, a.action_status)
        if a.action_status == "FINISHED":
            a.finished_action_ids.add(a.request.action.actionId)
        elif a.action_status == "FAILED" and not self.exec_errors:
            self.exec_errors.append(error_for_order(
                E_ORDER_EXECUTION_FAILED, f"mission '{a.request.mission_name}' terminó {state}",
                a.order.orderId, level="WARNING", action_id=a.request.action.actionId))

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
                if act.actionId == a.request.action.actionId:
                    st = a.action_status
                else:
                    st = "FINISHED" if act.actionId in a.finished_action_ids else "WAITING"
                ov.action_states.append(ActionState(act.actionId, st, act.actionType))
        ov.errors = [*self.order_errors, *self.exec_errors, *self.instant_errors]
        ov.instant_action_states = list(self.instant_states)
        return ov
