"""instantActions (H5, §6.4): acciones inmediatas fuera del ciclo de la order.

Soportadas (slugs literales de la norma, §6.10):

| actionType         | qué hace                                             |
|--------------------|------------------------------------------------------|
| `startPause`       | `driver.pause()` — el robot se detiene sin abortar   |
| `stopPause`        | `driver.resume()`                                    |
| `cancelOrder`      | `driver.cancel(job_id)` de la order activa           |
| `stateRequest`     | publica un `state` ya (fuera del tick)               |
| `factsheetRequest` | vuelve a publicar el `factsheet` retained             |

El resultado de cada una va a `state.instantActionStates[]` (FINISHED /
FAILED) y, si falla, a `state.errors[]` con `errorReferences: actionId`.
Se conservan hasta el siguiente mensaje `instantActions` (decisión 39).
"""
from __future__ import annotations

import logging
from typing import Callable

from fm.robot import Robot
from fm.vda5050.order import Action
from fm.vda5050.state import (E_INVALID_INSTANT_ACTION, E_NO_ORDER_TO_CANCEL, ActionState, Error,
                              error_for_order)

log = logging.getLogger("fm.instant")


class InstantFailed(Exception):
    def __init__(self, error_type: str, description: str):
        self.error_type, self.description = error_type, description
        super().__init__(f"{error_type}: {description}")


def apply_instant_actions(robot: Robot, actions: list[Action],
                          publish_state: Callable[[Robot], None],
                          publish_factsheet: Callable[[Robot], None] | None = None) -> None:
    """Ejecuta las actions en orden y deja el resultado en el tracker del
    robot. `publish_state` se llama al final (y en `stateRequest`);
    `publish_factsheet` en `factsheetRequest` (None = no soportado)."""
    tr = robot.orders
    tr.instant_states = []
    tr.instant_errors = []
    for a in actions:
        try:
            _apply_one(robot, a, publish_state, publish_factsheet)
            status = "FINISHED"
            log.info("[%s] instantAction %s/%s FINISHED", robot.serial, a.actionType, a.actionId)
        except InstantFailed as e:
            status = "FAILED"
            tr.instant_errors.append(_error(e.error_type, e.description, a, robot))
            log.warning("[%s] instantAction %s/%s FAILED: %s", robot.serial, a.actionType, a.actionId, e)
        except Exception as e:   # red, 4xx del robot...
            status = "FAILED"
            tr.instant_errors.append(_error(E_INVALID_INSTANT_ACTION,
                                            f"{type(e).__name__}: {str(e)[:120]}", a, robot))
            log.warning("[%s] instantAction %s/%s FAILED: %s", robot.serial, a.actionType, a.actionId, e)
        tr.instant_states.append(ActionState(a.actionId, status, a.actionType))
    publish_state(robot)


def _error(error_type: str, description: str, a: Action, robot: Robot) -> Error:
    active = robot.orders.active
    return error_for_order(error_type, description, active.order.orderId if active else None,
                           level="WARNING", action_id=a.actionId)


def _apply_one(robot: Robot, a: Action, publish_state: Callable[[Robot], None],
               publish_factsheet: Callable[[Robot], None] | None) -> None:
    d = robot.driver
    if a.actionType == "startPause":
        _require(d, "pause")()
    elif a.actionType == "stopPause":
        _require(d, "resume")()
    elif a.actionType == "cancelOrder":
        active = robot.orders.active
        if active is None or not active.alive:
            raise InstantFailed(E_NO_ORDER_TO_CANCEL, "no hay ninguna order activa que cancelar")
        d.cancel(active.job_id)
        robot.orders.cancel()
    elif a.actionType == "stateRequest":
        publish_state(robot)
    elif a.actionType == "factsheetRequest":
        if publish_factsheet is None:
            raise InstantFailed(E_INVALID_INSTANT_ACTION, "factsheet no disponible")
        publish_factsheet(robot)
    else:
        raise InstantFailed(E_INVALID_INSTANT_ACTION, f"actionType '{a.actionType}' no soportado")


def _require(driver, method: str):
    fn = getattr(driver, method, None)
    if fn is None:
        raise InstantFailed(E_INVALID_INSTANT_ACTION,
                            f"el driver {driver.manufacturer} no soporta '{method}'")
    return fn
