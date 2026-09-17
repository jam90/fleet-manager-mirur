"""Despacho de mensajes MQTT entrantes: `<serial>/order`, `fleet/order`
(asignador) y, en H5, `instantActions`. Corre en el hilo principal, drenando
`bus.inbox` al inicio de cada tick.
"""
from __future__ import annotations

import json
import logging

from fm.assigner import assign
from fm.config import FleetConfig
from fm.mqtt_bus import FLEET, MqttBus
from fm.orders import IgnoreOrder, pick_action
from fm.robot import Robot
from fm.vda5050.header import parse_topic, validate_header
from fm.vda5050.order import Order, OrderRejected, parse_order
from fm.vda5050.state import E_VALIDATION_FAILURE

log = logging.getLogger("fm.fleet")


class Dispatcher:
    def __init__(self, cfg: FleetConfig, robots: dict[str, Robot], bus: MqttBus, publish_state):
        self.cfg = cfg
        self.robots = robots
        self.bus = bus
        self.publish_state = publish_state      # callback(robot) → publica state ya (§6.6 eventos)
        self.last_fleet_order: tuple[str, int] | None = None

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
        header_err = validate_header(topic, data) if data is not None else "JSON malformado"

        if pt.subtopic == "order" and pt.serial == FLEET:
            self.handle_fleet_order(data, header_err)
        elif pt.subtopic == "order":
            self.handle_robot_order(pt.serial, data, header_err)
        elif pt.subtopic == "instantActions":
            log.warning("[%s] instantActions aún no implementadas (H5)", pt.serial)

    def handle_robot_order(self, serial: str, data, header_err: str | None) -> None:
        robot = self.robots.get(serial)
        if robot is None:
            log.warning("[fleet] order para robot desconocido '%s'", serial)
            return
        order_id = data.get("orderId") if isinstance(data, dict) else None
        try:
            if header_err:
                raise OrderRejected(E_VALIDATION_FAILURE, header_err)
            order = parse_order(data)
            req = robot.translate_order(order)
            robot.execute(order, req)
        except IgnoreOrder:
            log.info("[%s] order %s repetida: ignorada", serial, order_id)
            return
        except OrderRejected as e:
            robot.orders.reject(order_id, e)
        self.publish_state(robot)

    def handle_fleet_order(self, data, header_err: str | None) -> None:
        order_id = data.get("orderId") if isinstance(data, dict) else None
        update_id = data.get("orderUpdateId") if isinstance(data, dict) else None
        try:
            if header_err:
                raise OrderRejected(E_VALIDATION_FAILURE, header_err)
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
        robot = self.robots[result.serial]
        # El robot elegido puede aún rechazar (p.ej. mission inexistente en él).
        req = robot.translate_order(order)
        robot.execute(order, req)
        return robot
