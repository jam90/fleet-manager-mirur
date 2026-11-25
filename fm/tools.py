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

Una order puede valer con varias (punto 1b, decisión 211): `garra=dh,vgc10`
(o una lista JSON). Y el tipo de tarea puede decir con cuáles se hace
(`actions.<tipo>.tools`, decisión 212): una order sin el parámetro vale con
cualquiera de esas, y una que lo trae solo puede acotar dentro de ellas. El FM elige UNA por robot (`pick_tool`): la que ya
lleva, si es de la lista; si no, la primera de la lista que pueda ponerse.
La order que llega al driver lleva solo esa (`with_tool`): la mission
recibe un único código, como antes.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Collection, Mapping

from fm.vda5050.order import Action, ActionParameter, OrderRejected
from fm.vda5050.state import E_VALIDATION_FAILURE


def parse_tools(value: Any) -> list[str]:
    """Valor del parámetro → lista de herramientas, en el orden en que vienen
    (es la preferencia de quien manda la order) y sin repetidas. Vale una
    sola (`"dh"`), varias separadas por comas (`"dh, vgc10"`) o una lista."""
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    out: list[str] = []
    for v in items:
        name = str(v).strip()
        if name and name not in out:
            out.append(name)
    return out


def needed_tools(action: Action, actions: Mapping[str, dict], tools: Mapping[str, dict]) -> list[str] | None:
    """Las herramientas con que vale `action` (None = su action no usa
    herramienta; si no, al menos una). Sin el parámetro, las del tipo de
    tarea (`tools`, decisión 212); con él, las que pide, que tienen que ser
    de esas. Lanza `OrderRejected` (validationError) si falta el parámetro
    y el tipo de tarea no dice cuáles, o si pide una desconocida o que no
    vale para esa tarea: ningún robot la aceptaría."""
    spec = actions.get(action.actionType) or {}
    key = spec.get("tool_input")
    if not key:
        return None
    allowed = list(spec.get("tools") or [])
    given = action.params()
    names = parse_tools(given[key]) if key in given else []
    if not names:
        if allowed:
            return allowed
        raise OrderRejected(E_VALIDATION_FAILURE, f"falta el parámetro obligatorio '{key}' (herramienta)")
    unknown = [t for t in names if t not in tools]
    if unknown:
        raise OrderRejected(E_VALIDATION_FAILURE,
                            f"'{key}': herramienta '{unknown[0]}' desconocida; hay {sorted(tools)}")
    outside = [t for t in names if allowed and t not in allowed]
    if outside:
        raise OrderRejected(E_VALIDATION_FAILURE,
                            f"'{key}': {outside[0]} no vale para '{action.actionType}' (valen {', '.join(allowed)})")
    return names


def tools_label(tools: Collection[str]) -> str:
    """`dh` o `dh,vgc10`: como se escribe en la order (para la cola y la web)."""
    return ",".join(tools)


def _wanted(tools: str | Collection[str]) -> list[str]:
    return [tools] if isinstance(tools, str) else list(tools)


def usable_tools(tools: str | Collection[str], mounted: str | None, changer: Collection[str]) -> list[str]:
    """De las que valen, las que este robot puede usar, en el orden en que se
    prefieren: primero la que lleva (sin cambio), luego las de su cambiador
    en el orden de la order."""
    tools = _wanted(tools)
    first = [mounted] if mounted in tools else []
    return first + [t for t in tools if t in changer and t != mounted]


def tool_problem(tools: str | Collection[str], mounted: str | None, changer: Collection[str]) -> str:
    """Por qué un robot NO puede hacer una order que vale con `tools` (una o
    varias; "" = sí puede). `mounted`: la que lleva ahora ("" = ninguna;
    None = su driver no lo sabe); `changer`: las de su cambiador."""
    tools = _wanted(tools)
    if usable_tools(tools, mounted, changer):
        return ""
    want = " ni ".join(tools)
    if mounted is None:
        return f"no se sabe qué herramienta lleva y no tiene {want} en su cambiador"
    now = f"lleva {mounted}" if mounted else "no lleva herramienta"
    return f"{now} y no tiene {want} en su cambiador" if changer else f"{now}, no {want} (sin cambiador)"


def pick_tool(tools: str | Collection[str], mounted: str | None, changer: Collection[str],
              stock: Callable[[str], str] = lambda tool: "") -> tuple[str | None, str]:
    """La herramienta con que este robot haría la order: `(tool, "")`, o
    `(None, motivo)`. `stock(t)`: por qué no puede ir a por `t` al soporte
    ahora ("" = sí puede; `stock_problem`). Si la que prefiere no queda
    libre, prueba la siguiente: con `dh,vgc10` y la `dh` puesta en otro
    robot, va a por la `vgc10`."""
    usable = usable_tools(tools, mounted, changer)
    if not usable:
        return None, tool_problem(tools, mounted, changer)
    problems = []
    for t in usable:
        problem = stock(t)
        if not problem:
            return t, ""
        problems.append(problem)
    return None, "; ".join(problems)


def with_tool(action: Action, key: str, tool: str) -> Action:
    """Copia de `action` con `key` = `tool`: lo que recibe el driver cuando
    la order valía con varias (la mission recibe un único código), o cuando
    no lo traía y valen las del tipo de tarea (decisión 212)."""
    params = [ActionParameter(p.key, tool if p.key == key else p.value) for p in action.actionParameters]
    if key not in action.params():
        params.append(ActionParameter(key, tool))
    return replace(action, actionParameters=params)


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


# ------------------------------------------------ unidades en el soporte (decisión 196)
# Las herramientas de un soporte fijo (una mesa con el intercambiador) son de
# todos los robots que tienen acoplador en la muñeca: si una está puesta en un
# robot, los demás no pueden cogerla. `tools.<nombre>.count` dice cuántas
# unidades circulan por el soporte; sin `count` no se cuentan (como antes).
# Una herramienta fija de un robot sin acoplador (`tool:`) no es del soporte y
# no cuenta: ese robot no la deja nunca.

def tool_holders(tool: str, robots) -> list[str]:
    """Quién tiene ocupada una unidad de `tool` del soporte: los robots con
    `tool` en su cambiador que la llevan puesta, que la han pedido y aún no la
    llevan (van a buscarla), o de los que no se sabe qué llevan (por
    prudencia: si se diera por libre, otro robot iría a por una que no está).

    `robots`: iterable de `(serial, changer, mounted, requested)`, con
    `mounted` = la última que se le leyó ("" = ninguna; None = nunca se ha
    sabido) y `requested` = la que pide su order en curso (o None)."""
    out = []
    for serial, changer, mounted, requested in robots:
        if tool in changer and (mounted is None or mounted == tool or requested == tool):
            out.append(serial)
    return sorted(out)


def stock_problem(tool: str, count: int | None, holders, serial: str, mounted: str | None) -> str:
    """Por qué `serial` no puede ir a por `tool` al soporte ("" = sí puede, o
    no hace falta porque ya la lleva). Él mismo no cuenta entre los que la
    ocupan: si la pidió o no se sabe qué lleva, la unidad sería la suya."""
    if count is None or mounted == tool:
        return ""
    others = [h for h in holders if h != serial]
    if count - len(others) >= 1:
        return ""
    return f"no queda {tool} libre en el soporte (ocupada por {', '.join(others)})"
