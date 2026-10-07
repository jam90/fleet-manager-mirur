"""Traducción pura MiR ↔ core. Sin I/O: recibe snapshots e índices.

La parte con red (índices, cola) está en `driver.py`.

- `to_telemetry`: `MirStatus` (GET /status) → `Telemetry` normalizado.
- `from_vda_order` (H2): `Action` + índices nombre → GUID → body de `/mission_queue`.

El `state` VDA lo construye el core (`fm/vda5050/state_builder.py`) a partir
del `Telemetry`; aquí solo se interpreta lo que dice el MiR.
"""
from __future__ import annotations

import math
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
    # Para depurar "ocupado por mission ajena" sin abrir la web del MiR:
    # state_id y la entrada de cola en curso, tal cual los da /status.
    info.append(Info("MIR_STATUS", "DEBUG",
                     f"state_id={st.state_id} mission_queue_id={st.mission_queue_id}"
                     + (" (propia)" if st.mission_queue_id in own_queue_ids else "")))
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



def to_number(key: str, value) -> int | float:
    """Valor de un `number_inputs` → int/float para el JSON de `/mission_queue`.

    VDA 5050 no fija el tipo de `actionParameters.value` y cada emisor manda
    lo que le parece: la UI y `send_fleet_order.py` mandan texto (`"5"`),
    Node-RED puede mandar `5` o `5.0`. El MiR guarda el input de Blockly como
    número (p.ej. el `value` de *Set PLC register*), así que lo normalizamos
    aquí y rechazamos lo que no lo sea antes de que llegue al robot.
    Enteros como int: los registros PLC 1–100 del MiR son enteros.
    """
    if isinstance(value, bool):     # True es int en Python, pero no es un número aquí
        raise OrderRejected(E_VALIDATION_FAILURE, f"'{key}' debe ser un número, no {value!r}")
    try:
        num = float(str(value).strip().replace(",", "."))   # "2,5" también vale
    except ValueError:
        raise OrderRejected(E_VALIDATION_FAILURE, f"'{key}' debe ser un número, no {value!r}") from None
    if not math.isfinite(num):
        raise OrderRejected(E_VALIDATION_FAILURE, f"'{key}' debe ser un número finito, no {value!r}")
    return int(num) if num.is_integer() else num


def _fmt(x: float | None) -> str:
    """Límite para mensajes: 10.0 → "10", None → "∞"."""
    return "∞" if x is None else f"{x:g}"


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
        # Allowlist de la action: se suma a la del robot (hay que pasar las dos).
        if acfg.positions_allowlist is not None and name not in acfg.positions_allowlist:
            raise OrderRejected(E_NO_ROUTE_TO_TARGET,
                                f"position '{name}' no admitida en '{action.actionType}'; "
                                f"admite {sorted(acfg.positions_allowlist)}")
        pguid = positions.get(name)
        if pguid is None:
            raise OrderRejected(E_NO_ROUTE_TO_TARGET, f"[{robot.serial}] position '{name}' no existe")
        params.append({"id": key, "value": pguid})

    for key in acfg.required_inputs:
        if key not in given:     # 0/False/"" son válidos: solo cuenta ausente
            raise OrderRejected(E_VALIDATION_FAILURE, f"falta el parámetro obligatorio '{key}'")
        params.append({"id": key, "value": given[key]})

    # Por input_name, igual que target_pos: el GUID interno del parámetro
    # (distinto en cada robot) no se usa nunca (CLAUDE.md §9.1).
    for key in acfg.number_inputs:
        if key not in given:
            raise OrderRejected(E_VALIDATION_FAILURE, f"falta el parámetro obligatorio '{key}' (número)")
        value = to_number(key, given[key])
        # El rango de la UI no basta: la order puede venir de Node-RED o de un script.
        r = acfg.number_ranges.get(key)
        if r is not None and ((r.min is not None and value < r.min) or (r.max is not None and value > r.max)):
            raise OrderRejected(E_VALIDATION_FAILURE,
                                f"'{key}' = {value} fuera de rango [{_fmt(r.min)}, {_fmt(r.max)}]")
        params.append({"id": key, "value": value})

    # El resto solo si la mission expone ese input_name (compatibilidad hacia delante).
    handled = set(acfg.position_inputs) | set(acfg.required_inputs) | set(acfg.number_inputs)
    for key, value in given.items():
        if key not in handled and key in exposed:
            params.append({"id": key, "value": value})

    return MissionRequest(action, acfg.mission, guid, params)
