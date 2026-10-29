"""Despacho de mensajes MQTT entrantes: `<serial>/order`, `fleet/order`
(asignador) e `instantActions`. Corre en el hilo principal, drenando
`bus.inbox` al inicio de cada tick.
"""
from __future__ import annotations

import json
import logging

from fm.assigner import assign
from fm.config import FleetConfig
from fm.mqtt_bus import FLEET, MqttBus
from fm.instant import apply_instant_actions
from fm.orders import IgnoreOrder, pick_action
from fm.robot import Robot
from fm.vda5050.header import parse_topic, validate_header
from fm.vda5050.order import Order, OrderRejected, parse_instant_actions, parse_order
from fm.vda5050.schemas import validation_errors
from fm.vda5050.state import E_NO_MOBILE_ROBOT_AVAILABLE, E_VALIDATION_FAILURE, error_for_order

log = logging.getLogger("fm.fleet")

SCHEMA_PROBLEMS_SHOWN = 3     # el motivo de rechazo cabe en un log y en la UI


def schema_problem(subtopic: str, data) -> str | None:
    """Motivo legible si `data` no cumple el schema de `subtopic`; None si valida."""
    problems = validation_errors(subtopic, data)
    if not problems:
        return None
    more = len(problems) - SCHEMA_PROBLEMS_SHOWN
    return ("no cumple el schema VDA 5050: " + "; ".join(problems[:SCHEMA_PROBLEMS_SHOWN])
            + (f" (y {more} más)" if more > 0 else ""))


class Dispatcher:
    def __init__(self, cfg: FleetConfig, robots: dict[str, Robot], bus: MqttBus, publish_state,
                 publish_factsheet=None, validate: bool = False):
        self.cfg = cfg
        self.robots = robots
        self.bus = bus
        self.publish_state = publish_state      # callback(robot) → publica state ya (§6.6 eventos)
        self.publish_factsheet = publish_factsheet   # callback(robot) → factsheet retained (H5 factsheetRequest)
        self.last_fleet_order: tuple[str, int] | None = None
        # `--validate` (decisión 122): además de lo que comprueba `parse_order`,
        # cada order/instantActions entrante se valida contra el JSON Schema
        # oficial antes de procesarlo.
        self.validate = validate

    def subscribe(self) -> None:
        # Cada robot vive bajo el manufacturer de su driver: se escucha a todos
        # y `handle()` filtra por (manufacturer, serial) configurados.
        self.bus.subscribe("vda5050/v3/+/+/order")
        self.bus.subscribe("vda5050/v3/+/+/instantActions")

    def drain(self) -> None:
        while not self.bus.inbox.empty():
            topic, payload = self.bus.inbox.get_nowait()
            try:
                self.handle(topic, payload)
            except Exception:
                log.exception("[fleet] error procesando %s", topic)

    # ------------------------------------------------------------- despacho
    def handle(self, topic: str, payload: bytes) -> None:
        pt = parse_topic(topic)
        if pt is None:
            return
        if not self.bus.is_known(pt.manufacturer, pt.serial):
            log.debug("[fleet] %s ignorado: (%s, %s) no es un robot de esta flota",
                      topic, pt.manufacturer, pt.serial)
            return
        try:
            data = json.loads(payload)
        except ValueError:
            data = None
        invalid = validate_header(topic, data) if data is not None else "JSON malformado"
        if invalid is None and self.validate and pt.subtopic in ("order", "instantActions"):
            invalid = schema_problem(pt.subtopic, data)

        if pt.subtopic == "order" and pt.serial == FLEET:
            self.handle_fleet_order(data, invalid)
        elif pt.subtopic == "order":
            self.handle_robot_order(pt.serial, data, invalid)
        elif pt.subtopic == "instantActions":
            self.handle_instant_actions(pt.serial, data, invalid)

    def handle_robot_order(self, serial: str, data, invalid: str | None) -> None:
        robot = self.robots.get(serial)
        if robot is None:
            log.warning("[fleet] order para robot desconocido '%s'", serial)
            return
        order_id = data.get("orderId") if isinstance(data, dict) else None
        try:
            if invalid:
                raise OrderRejected(E_VALIDATION_FAILURE, invalid)
            order = parse_order(data)
            req = robot.translate_order(order)
            robot.execute(order, req)
        except IgnoreOrder:
            log.info("[%s] order %s repetida: ignorada", serial, order_id)
            return
        except OrderRejected as e:
            robot.orders.reject(order_id, e)
        self.publish_state(robot)

    def handle_instant_actions(self, serial: str, data, invalid: str | None) -> None:
        robot = self.robots.get(serial)
        if robot is None:
            log.warning("[fleet] instantActions para robot desconocido '%s'", serial)
            return
        try:
            if invalid:
                raise OrderRejected(E_VALIDATION_FAILURE, invalid)
            actions = parse_instant_actions(data)
        except OrderRejected as e:
            # Mensaje inválido: no hay actionId al que referirse; error suelto en state.
            robot.orders.instant_errors = [error_for_order(e.error_type, e.description, level="WARNING")]
            log.warning("[%s] instantActions rechazadas: %s", serial, e)
            self.publish_state(robot)
            return
        apply_instant_actions(robot, actions, self.publish_state, self.publish_factsheet)

    def handle_fleet_order(self, data, invalid: str | None) -> None:
        order_id = data.get("orderId") if isinstance(data, dict) else None
        update_id = data.get("orderUpdateId") if isinstance(data, dict) else None
        try:
            if invalid:
                raise OrderRejected(E_VALIDATION_FAILURE, invalid)
            order = parse_order(data)
            if self.last_fleet_order == (order.orderId, order.orderUpdateId):
                log.info("[fleet] fleet/order %s/%d repetida: ignorada", order.orderId, order.orderUpdateId)
                return
            robot = self._assign_and_execute(order)
        except OrderRejected as e:
            log.warning("[fleet] fleet/order %s rechazada: %s", order_id, e)
            self.bus.publish(FLEET, "order_response", {
                "orderId": order_id or "", "orderUpdateId": update_id if isinstance(update_id, int) else 0,
                "status": "REJECTED", "errorType": e.error_type, "errorDescription": e.description})
            return
        self.last_fleet_order = (order.orderId, order.orderUpdateId)
        self.bus.publish(FLEET, "order_response", {
            "orderId": order.orderId, "orderUpdateId": order.orderUpdateId,
            "status": "ASSIGNED", "assignedSerial": robot.serial,
            "description": f"asignado a {robot.serial}"})
        log.info("[fleet] fleet/order %s → %s", order.orderId, robot.serial)
        self.publish_state(robot)

    def _assign_and_execute(self, order: Order) -> Robot:
        # Una sola action por order (se valida aquí para que el rechazo sea
        # VALIDATION_FAILURE y no un rechazo por robot).
        action = pick_action(order)
        snaps = {s: r.snapshot() for s, r in self.robots.items()}
        result = assign(action.actionType, snaps, self.cfg)
        if not result.ok:
            raise OrderRejected(result.error_type, result.reason)
        # El asignador solo mira batería y ocupación; el robot elegido aún
        # puede rechazar al traducir (p.ej. no tiene grabada la mission). En
        # ese caso se prueba el siguiente del ranking (decisión 89). Traducir
        # no toca la red ni el robot, así que probar varios no tiene efectos.
        failures: dict[str, OrderRejected] = {}
        for serial in result.ranking:
            robot = self.robots[serial]
            try:
                req = robot.translate_order(order)
            except OrderRejected as e:
                # VALIDATION_FAILURE es culpa de la order (falta un parámetro,
                # no es un número…): ningún otro robot la aceptaría.
                if e.error_type == E_VALIDATION_FAILURE:
                    raise
                log.info("[fleet] %s no puede con %s (%s): pruebo el siguiente",
                         serial, order.orderId, e)
                failures[serial] = e
                continue
            # `execute` NO se reintenta en otro robot: tras un timeout el MiR
            # pudo encolarla igual y acabarían dos robots con la misma order.
            robot.execute(order, req)
            return robot
        raise self._all_failed(result.rejections, failures)

    @staticmethod
    def _all_failed(rejections: dict[str, str], failures: dict[str, OrderRejected]) -> OrderRejected:
        """Ningún candidato pudo traducir. Si todos fallaron por lo mismo se
        conserva ese errorType (con un solo candidato, el rechazo es el de
        siempre); si no, NO_MOBILE_ROBOT_AVAILABLE. La descripción lleva el
        motivo de cada robot, también de los que descartó el asignador."""
        types = {e.error_type for e in failures.values()}
        error_type = types.pop() if len(types) == 1 else E_NO_MOBILE_ROBOT_AVAILABLE
        reasons = {**rejections, **{s: e.description for s, e in failures.items()}}
        detail = "; ".join(f"{s}: {reasons[s]}" for s in sorted(reasons))
        return OrderRejected(error_type, f"ningún robot puede atender la order: {detail}")
