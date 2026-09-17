"""Construcción del `state` VDA 5050 a partir de lo que sabe el core.

Dos fuentes, ninguna de marca:

- `Telemetry` (del driver): lo que el robot dice de sí mismo.
- `StateOverlay` (del `OrderTracker`): lo que el FM sabe y el robot no — la
  order VDA en curso, sus actions, los errores de rechazo, la auto-carga.

Al vivir en el core, un driver nuevo no puede publicar un `state` mal
formado: solo aporta `Telemetry`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fm.vda5050.state import (E_ROBOT_UNREACHABLE, ActionState, Error, Info,
                              MobileRobotPosition, PowerSupply, SafetyState, State)

if TYPE_CHECKING:   # solo para tipos: evita el import circular adapters ↔ vda5050
    from fm.adapters.base import Telemetry


@dataclass
class StateOverlay:
    """Lo que el `Telemetry` no sabe y el FM sí. H1 lo deja vacío; H2/H4/H5
    lo rellenan desde el tracker de cada robot."""
    order_id: str = ""
    order_update_id: int = 0
    action_states: list[ActionState] = field(default_factory=list)
    instant_action_states: list[ActionState] = field(default_factory=list)
    errors: list[Error] = field(default_factory=list)
    information: list[Info] = field(default_factory=list)
    charging: bool | None = None     # None = fiarse del driver


def _charging(t: Telemetry, overlay: StateOverlay) -> bool:
    """Prioridad: (a) el FM sabe que su job de carga está en marcha (overlay);
    (b) lo que diga el driver; (c) False si nadie lo sabe."""
    if overlay.charging is not None:
        return overlay.charging
    return bool(t.charging)


def to_vda_state(header: dict, t: Telemetry | None, overlay: StateOverlay | None = None,
                 error: str | None = None) -> State:
    """Construye el `state` VDA. Si `t` es None (robot inalcanzable) se publica
    igual con `ROBOT_UNREACHABLE` en `errors[]` y sin posición."""
    overlay = overlay or StateOverlay()
    s = State(**header)
    s.orderId = overlay.order_id
    s.orderUpdateId = overlay.order_update_id
    s.actionStates = list(overlay.action_states)
    s.instantActionStates = list(overlay.instant_action_states)
    s.errors = list(overlay.errors)
    s.information = list(overlay.information)

    if t is None:
        s.errors.append(Error(E_ROBOT_UNREACHABLE, "URGENT", error or "sin respuesta del robot"))
        s.powerSupply = PowerSupply(0.0, False)
        return s

    s.driving = t.driving
    s.paused = t.paused
    s.operatingMode = t.operating_mode
    if t.pose is not None and t.map_id is not None:
        x, y, theta = t.pose
        s.mobileRobotPosition = MobileRobotPosition(x, y, theta, t.map_id, localized=True)
    s.powerSupply = PowerSupply(t.battery, _charging(t, overlay))
    s.safetyState = SafetyState(activeEmergencyStop="MANUAL" if t.emergency_stop else "NONE",
                                fieldViolation=False)
    s.errors.extend(t.errors)
    s.information.extend(t.information)
    return s
