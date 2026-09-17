"""Traducción pura MiR ↔ core. Sin I/O: recibe snapshots e índices.

La parte con red (índices, cola) está en `driver.py`.

- `to_telemetry`: `MirStatus` (GET /status) → `Telemetry` normalizado.
- `from_vda_order` (H2): `Action` + índices nombre → GUID → body de `/mission_queue`.

El `state` VDA lo construye el core (`fm/vda5050/state_builder.py`) a partir
del `Telemetry`; aquí solo se interpreta lo que dice el MiR.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Collection

from fm.adapters.base import Telemetry
from fm.adapters.mir.config import MirRobotConfig
from fm.adapters.mir.client import (STATE_EMERGENCY_STOP, STATE_ERROR, STATE_EXECUTING,
                                    STATE_MANUAL, STATE_PAUSE, MirStatus)
from fm.vda5050.order import Action, OrderRejected
from fm.vda5050.state import (E_INVALID_ORDER_ACTION, E_NO_ROUTE_TO_TARGET,
                              E_VALIDATION_FAILURE, Error, Info)

# state_id del MiR en los que NO se aceptan orders (decisión 16/18).
UNAVAILABLE_STATES = (STATE_MANUAL, STATE_ERROR, STATE_EMERGENCY_STOP)


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


def to_telemetry(st: MirStatus, own_queue_ids: Collection[int] = ()) -> Telemetry:
    """`MirStatus` → `Telemetry`. `own_queue_ids` son las entradas de
    `mission_queue` que lanzó el FM: cualquier otra mission en marcha es ajena
    (lanzada desde la web) y bloquea el robot (decisión 16)."""
    running = st.mission_queue_id is not None or st.state_id == STATE_EXECUTING
    foreign = running and st.mission_queue_id not in own_queue_ids
    info = [Info("MISSION", "INFO", st.mission_text)] if st.mission_text else []
    return Telemetry(
        battery=st.battery_percentage,
        pose=(st.x, st.y, st.theta),
        map_id=st.map_id,
        driving=st.state_id == STATE_EXECUTING,
        paused=st.state_id == STATE_PAUSE,
        # Heurística sobre /status PENDIENTE DE VALIDAR en el dock (CLAUDE.md
        # §5.2): de momento el MiR no informa y decide el overlay (auto-carga).
        charging=None,
        operating_mode="MANUAL" if st.state_id == STATE_MANUAL else "AUTOMATIC",
        emergency_stop=st.state_id == STATE_EMERGENCY_STOP,
        available=st.state_id not in UNAVAILABLE_STATES,
        foreign_busy=foreign,
        unavailable_reason=f"robot en estado {st.state_text} (state_id={st.state_id})",
        errors=_mir_errors(st),
        information=info,
    )


# ---------------------------------------------------------------- order → mission


@dataclass
class MissionRequest:
    """Lo que hay que POSTear a `/mission_queue` para ejecutar una action."""
    action: Action
    mission_name: str
    mission_guid: str
    parameters: list[dict]          # [{"id": input_name, "value": ...}]



def from_vda_order(action: Action, robot: MirRobotConfig, missions: dict[str, str],
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
