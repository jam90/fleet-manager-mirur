"""Herramientas de los robots (decisión 184): funciones puras para el
asignador, la cola y las orders dirigidas.

Un robot puede hacer una order que pide la herramienta T si la lleva
montada ahora (la lee su driver: `Telemetry.tool`) o si T está en su
cambiador (`robots.<serial>.tool_changer`): el cambio lo hace la propia
mission del robot, que recibe T como parámetro. El FM solo elige robot y,
entre los que valen, prefiere el que ya la lleva (sin cambio).

Qué herramienta pide una order lo dice su parámetro `tool_input` (p. ej.
`garra`). Qué herramienta necesita cada pieza no es cosa del FM: lo decide
quien manda la order (el MES, otro servicio).
"""
from __future__ import annotations

from typing import Collection, Mapping

from fm.vda5050.order import Action, OrderRejected
from fm.vda5050.state import E_VALIDATION_FAILURE


def needed_tool(action: Action, actions: Mapping[str, dict], tools: Mapping[str, dict]) -> str | None:
    """La herramienta que pide `action` (None = su action no usa herramienta).
    Lanza `OrderRejected` (validationError) si falta el parámetro o la
    herramienta no está en el catálogo: ningún robot la aceptaría."""
    key = (actions.get(action.actionType) or {}).get("tool_input")
    if not key:
        return None
    given = action.params()
    if key not in given:
        raise OrderRejected(E_VALIDATION_FAILURE, f"falta el parámetro obligatorio '{key}' (herramienta)")
    tool = str(given[key])
    if tool not in tools:
        raise OrderRejected(E_VALIDATION_FAILURE,
                            f"'{key}': herramienta '{tool}' desconocida; hay {sorted(tools)}")
    return tool


def tool_problem(tool: str, mounted: str | None, changer: Collection[str]) -> str:
    """Por qué un robot NO puede hacer una order que pide `tool` ("" = sí
    puede). `mounted`: la que lleva ahora ("" = ninguna; None = su driver no
    lo sabe); `changer`: las de su cambiador."""
    if mounted == tool or tool in changer:
        return ""
    if mounted is None:
        return f"no se sabe qué herramienta lleva y no tiene {tool} en su cambiador"
    now = f"lleva {mounted}" if mounted else "no lleva herramienta"
    return f"{now} y no tiene {tool} en su cambiador" if changer else f"{now}, no {tool} (sin cambiador)"


def changer_notice(changer: Collection[str], mounted: str | None) -> str:
    """Aviso al arrancar el FM para un robot con cambiador: lo que dice
    `fleet.yaml` que tiene. Si alguien quitó una herramienta y no lo cambió
    en la config, el FM mandaría ahí la order y fallaría el cambio en la
    mission: quien lo arranca tiene que comprobarlo."""
    now = "no se sabe cuál lleva" if mounted is None else f"lleva {mounted}" if mounted else "no lleva ninguna"
    return f"revisa que su cambiador tiene {', '.join(sorted(changer))} (lo dice fleet.yaml); ahora {now}"


def tool_name(code: int, codes: Mapping[str, int]) -> str:
    """Código leído del robot → nombre del catálogo. 0 = ninguna (""); uno
    que no está en el catálogo sale tal cual, para verlo y que no coincida
    con ninguna herramienta pedida."""
    if code == 0:
        return ""
    return next((name for name, c in codes.items() if c == code), f"código {code}")
