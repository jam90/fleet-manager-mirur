"""Asignador de flota: función pura `assign(order_action_type, snapshots, cfg)`.

Filtros en orden (CLAUDE.md §6): soporta el actionType → (si la order pide
herramienta) la lleva o la tiene en su cambiador → batería ≥ battery_min → no
ocupado → (si las herramientas del soporte están contadas, decisión 196) la
lleva o queda alguna libre → (opcional) no cargando si hay otro libre. Ranking: primero los que
ya llevan la herramienta pedida (sin cambio, decisión 184), luego mayor
batería, desempate alfabético. Sin candidatos → rechazo con el detalle por robot.

Si la order vale con varias herramientas (decisión 211), cada robot usa la
suya (`fm.tools.pick_tool`): la que lleva si es de la lista, o la primera
que pueda ponerse. Va en `AssignResult.tools`, para que el `Dispatcher` se
la pase al robot elegido.

Devuelve el ranking completo, no solo el mejor: si el elegido no puede
traducir la order (p.ej. le falta la mission), el `Dispatcher` prueba el
siguiente (decisión 89). La función sigue sin saber nada de missions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Collection, Mapping

from fm.config import FleetConfig
from fm.tools import pick_tool, stock_problem, tool_problem
from fm.vda5050.state import E_NO_MOBILE_ROBOT_AVAILABLE

# Comienzo del motivo sin candidatos. La cola lo cambia por «ahora no puede…»
# mientras la order espera (fm/fleet.py): no es un rechazo.
NOBODY = "ningún robot puede atender la order: "


@dataclass(frozen=True)
class RobotSnapshot:
    serial: str
    battery: float
    busy: bool
    available: bool = True            # False = MANUAL / Error / EmergencyStop / sin REST
    charging: bool = False
    position: tuple[float, float] | None = None   # hueco para "menor distancia" (futuro)
    reason: str = ""                  # por qué no está disponible, si se sabe (p. ej. fuera del site)
    tool: str | None = None           # herramienta montada ("" = ninguna; None = el driver no lo sabe)


@dataclass
class AssignResult:
    serial: str | None
    reason: str
    error_type: str | None = None
    rejections: dict[str, str] = field(default_factory=dict)
    ranking: list[str] = field(default_factory=list)   # candidatos en orden; ranking[0] == serial
    tools: dict[str, str] = field(default_factory=dict)  # candidato → herramienta con que la haría

    @property
    def ok(self) -> bool:
        return self.serial is not None


def assign(action_type: str, snapshots: dict[str, RobotSnapshot], cfg: FleetConfig,
           tool: str | Collection[str] | None = None,
           stock: Mapping[str, tuple[int | None, list[str]]] = {}) -> AssignResult:
    """`tool`: la herramienta o herramientas con que vale la order
    (`fm.tools.needed_tools`), o None. `stock`: `(count, holders)` de cada
    una en el soporte (decisión 196; `fm.tools.tool_holders`); sin entrada o
    con `count` None = no se cuenta."""
    tools = [tool] if isinstance(tool, str) else list(tool or [])
    rejections: dict[str, str] = {}
    candidates: list[RobotSnapshot] = []
    chosen: dict[str, str] = {}
    for serial in sorted(snapshots):
        s = snapshots[serial]
        rcfg = cfg.robots.get(serial)
        if rcfg is None or action_type not in rcfg.action_types:
            rejections[serial] = f"no soporta '{action_type}'"
        elif tools and tool_problem(tools, s.tool, rcfg.tool_changer):
            rejections[serial] = tool_problem(tools, s.tool, rcfg.tool_changer)
        elif not s.available:
            rejections[serial] = s.reason or "no disponible (manual/error/sin REST)"
        elif s.battery < rcfg.battery_min:
            rejections[serial] = f"batería {s.battery:.1f}% < mínimo {rcfg.battery_min:.0f}%"
        elif s.busy:
            rejections[serial] = "ocupado"
        elif tools:
            t, problem = pick_tool(tools, s.tool, rcfg.tool_changer,
                                   lambda t: stock_problem(t, *stock.get(t, (None, [])), serial, s.tool))
            if t is None:
                rejections[serial] = problem
            else:
                chosen[serial] = t
                candidates.append(s)
        else:
            candidates.append(s)

    # Ranking: el que ya lleva la herramienta pedida primero (cambiarla lleva
    # su tiempo); después, más batería; alfabético en empate. Sustituir la
    # batería por "menor distancia al destino" (usar `position`) cuando haga falta.
    candidates.sort(key=lambda c: (bool(tools) and c.tool not in tools, -c.battery, c.serial))

    if cfg.prefer_not_charging and candidates:
        free = [c for c in candidates if not c.charging]
        if free:
            for c in candidates:
                if c.charging:
                    rejections[c.serial] = "cargando (hay otro libre)"
            # "Preferir" no es "descartar": los que cargan van al final del
            # ranking, por si ninguno de los libres puede traducir la order.
            candidates = free + [c for c in candidates if c.charging]

    if not candidates:
        detail = "; ".join(f"{k}: {v}" for k, v in rejections.items()) or "sin robots"
        return AssignResult(None, f"{NOBODY}{detail}",
                            E_NO_MOBILE_ROBOT_AVAILABLE, rejections)

    best = candidates[0]
    return AssignResult(best.serial, f"asignado a {best.serial} ({best.battery:.1f}%)",
                        rejections=rejections, ranking=[c.serial for c in candidates],
                        tools={c.serial: chosen[c.serial] for c in candidates if c.serial in chosen})
