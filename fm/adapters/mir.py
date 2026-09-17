"""Traducción pura MiR ↔ VDA 5050. Sin I/O: recibe snapshots e índices.

- `to_vda_state`: `MirStatus` (+ lo que el FM sabe de la order en curso) → `State`.
- `from_vda_order` (H2): `Order` + índices nombre → GUID → body de `/mission_queue`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fm.mir_client import (STATE_EMERGENCY_STOP, STATE_ERROR, STATE_EXECUTING,
                           STATE_MANUAL, STATE_PAUSE, MirStatus)
from fm.vda5050.state import (E_MIR_UNREACHABLE, ActionState, Error, Info,
                              MobileRobotPosition, PowerSupply, SafetyState, State)


@dataclass
class StateOverlay:
    """Lo que el `/status` del MiR no sabe y el FM sí: la order VDA en curso,
    las actions, los errores de rechazo y la auto-carga. H1 lo deja vacío;
    H2/H4/H5 lo rellenan desde el tracker de cada robot."""
    order_id: str = ""
    order_update_id: int = 0
    action_states: list[ActionState] = field(default_factory=list)
    instant_action_states: list[ActionState] = field(default_factory=list)
    errors: list[Error] = field(default_factory=list)
    information: list[Info] = field(default_factory=list)
    charging: bool | None = None     # None = deducir del /status (pendiente de validar, §5.2)


def _operating_mode(state_id: int) -> str:
    return "MANUAL" if state_id == STATE_MANUAL else "AUTOMATIC"


def _mir_errors(st: MirStatus) -> list[Error]:
    """`errors[]` del MiR → `MIR_<code>`. FATAL si el robot está en Error (12):
    requiere intervención; URGENT si el MiR informa errores sin bloquearse."""
    level = "FATAL" if st.state_id == STATE_ERROR else "URGENT"
    out = []
    for e in st.errors:
        code = e.get("code", "unknown")
        out.append(Error(f"MIR_{code}", level, str(e.get("description", "")),
                         errorHint=e.get("module")))
    if st.state_id == STATE_ERROR and not out:
        out.append(Error("MIR_STATE_ERROR", "FATAL", st.state_text or "robot en estado Error"))
    return out


def _charging(st: MirStatus, overlay: StateOverlay) -> bool:
    """Prioridad: (a) el FM sabe que su mission de carga está Executing;
    (b) heurística sobre /status. La (b) está PENDIENTE DE VALIDAR con robot
    real en el dock (CLAUDE.md §5.2): de momento, `charging` solo es True por (a).
    """
    if overlay.charging is not None:
        return overlay.charging
    return False


def to_vda_state(header: dict, st: MirStatus | None, overlay: StateOverlay | None = None,
                 rest_error: str | None = None) -> State:
    """Construye el `state` VDA. Si `st` es None (REST caído) se publica igual
    con `MIR_REST_UNREACHABLE` en `errors[]` y sin posición."""
    overlay = overlay or StateOverlay()
    s = State(**header)
    s.orderId = overlay.order_id
    s.orderUpdateId = overlay.order_update_id
    s.actionStates = list(overlay.action_states)
    s.instantActionStates = list(overlay.instant_action_states)
    s.errors = list(overlay.errors)
    s.information = list(overlay.information)

    if st is None:
        s.errors.append(Error(E_MIR_UNREACHABLE, "URGENT", rest_error or "sin respuesta REST"))
        s.powerSupply = PowerSupply(0.0, False)
        return s

    s.driving = st.state_id == STATE_EXECUTING
    s.paused = st.state_id == STATE_PAUSE
    s.operatingMode = _operating_mode(st.state_id)
    s.mobileRobotPosition = MobileRobotPosition(st.x, st.y, st.theta, st.map_id, localized=True)
    s.powerSupply = PowerSupply(st.battery_percentage, _charging(st, overlay))
    s.safetyState = SafetyState(
        activeEmergencyStop="MANUAL" if st.state_id == STATE_EMERGENCY_STOP else "NONE",
        fieldViolation=False)   # el MiR no lo expone en /status
    s.errors.extend(_mir_errors(st))
    if st.mission_text:
        s.information.append(Info("MISSION", "INFO", st.mission_text))
    return s


# ---------------------------------------------------------------- order → mission
from fm.config import RobotConfig  # noqa: E402
from fm.vda5050.order import Action, Order, OrderRejected  # noqa: E402
from fm.vda5050.state import (E_INVALID_ORDER_ACTION, E_NO_ROUTE_TO_TARGET,  # noqa: E402
                              E_VALIDATION_FAILURE)


@dataclass
class MissionRequest:
    """Lo que hay que POSTear a `/mission_queue` para ejecutar una action."""
    action: Action
    mission_name: str
    mission_guid: str
    parameters: list[dict]          # [{"id": input_name, "value": ...}]


def pick_action(order: Order, done_action_ids: set[str] = frozenset()) -> Action:
    """La única action a ejecutar (decisión 10/11): entre las de los nodos
    released, la primera cuyo actionId no esté ya FINISHED. Más de una
    pendiente → VALIDATION_FAILURE (mejor rechazar que ejecutar a medias)."""
    pending = [a for a in order.released_actions() if a.actionId not in done_action_ids]
    if not pending:
        raise OrderRejected(E_VALIDATION_FAILURE, "la order no contiene ninguna action que ejecutar")
    if len(pending) > 1:
        raise OrderRejected(E_VALIDATION_FAILURE,
                            f"solo se admite una action por order; llegan {len(pending)}: "
                            + ", ".join(f"{a.actionType}/{a.actionId}" for a in pending))
    return pending[0]


def from_vda_order(action: Action, robot: RobotConfig, missions: dict[str, str],
                   positions: dict[str, str], mission_inputs: dict[str, set[str]],
                   allowlist: set[str] | None = None) -> MissionRequest:
    """`Action` VDA → `MissionRequest` usando los índices DE ESE robot.

    Función pura: no toca la red. Los errores salen como `OrderRejected` con
    el errorType v3 adecuado (ver tabla en CLAUDE.md §5.8 y decisión 1).
    """
    acfg = robot.actions.get(action.actionType)
    if acfg is None:
        raise OrderRejected(E_INVALID_ORDER_ACTION,
                            f"[{robot.serial}] actionType '{action.actionType}' no soportado; "
                            f"soporta {sorted(robot.actions)}")
    guid = missions.get(acfg.mission)
    if guid is None:
        raise OrderRejected(E_NO_ROUTE_TO_TARGET,
                            f"[{robot.serial}] la mission '{acfg.mission}' no existe en el robot")
    given = action.params()
    exposed = mission_inputs.get(guid, set())
    params: list[dict] = []

    for key in acfg.position_inputs:
        if key not in given:
            raise OrderRejected(E_VALIDATION_FAILURE, f"falta el parámetro obligatorio '{key}' (position)")
        name = str(given[key])
        if allowlist is not None and name not in allowlist:
            raise OrderRejected(E_NO_ROUTE_TO_TARGET, f"position '{name}' fuera de la allowlist")
        pguid = positions.get(name)
        if pguid is None:
            raise OrderRejected(E_NO_ROUTE_TO_TARGET, f"[{robot.serial}] position '{name}' no existe")
        params.append({"id": key, "value": pguid})

    for key in acfg.required_inputs:
        if key not in given:     # 0/False/"" son válidos: solo cuenta ausente
            raise OrderRejected(E_VALIDATION_FAILURE, f"falta el parámetro obligatorio '{key}'")
        params.append({"id": key, "value": given[key]})

    # El resto solo si la mission expone ese input_name (compatibilidad hacia delante).
    handled = set(acfg.position_inputs) | set(acfg.required_inputs)
    for key, value in given.items():
        if key not in handled and key in exposed:
            params.append({"id": key, "value": value})

    return MissionRequest(action, acfg.mission, guid, params)
