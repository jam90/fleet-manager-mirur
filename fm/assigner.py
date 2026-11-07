"""Asignador de flota: función pura `assign(order_action_type, snapshots, cfg)`.

Filtros en orden (CLAUDE.md §6): soporta el actionType → (si la order pide
herramienta) la lleva o la tiene en su cambiador → batería ≥ battery_min → no
ocupado → (opcional) no cargando si hay otro libre. Ranking: primero los que
ya llevan la herramienta pedida (sin cambio, decisión 184), luego mayor
batería, desempate alfabético. Sin candidatos → rechazo con el detalle por robot.

Devuelve el ranking completo, no solo el mejor: si el elegido no puede
traducir la order (p.ej. le falta la mission), el `Dispatcher` prueba el
siguiente (decisión 89). La función sigue sin saber nada de missions.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fm.config import FleetConfig
from fm.tools import tool_problem
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

    @property
    def ok(self) -> bool:
        return self.serial is not None


def assign(action_type: str, snapshots: dict[str, RobotSnapshot], cfg: FleetConfig,
           tool: str | None = None) -> AssignResult:
    """`tool`: la herramienta que pide la order (`fm.tools.needed_tool`), o None."""
    rejections: dict[str, str] = {}
    candidates: list[RobotSnapshot] = []
    for serial in sorted(snapshots):
        s = snapshots[serial]
        rcfg = cfg.robots.get(serial)
        if rcfg is None or action_type not in rcfg.action_types:
            rejections[serial] = f"no soporta '{action_type}'"
        elif tool and tool_problem(tool, s.tool, rcfg.tool_changer):
            rejections[serial] = tool_problem(tool, s.tool, rcfg.tool_changer)
        elif not s.available:
            rejections[serial] = s.reason or "no disponible (manual/error/sin REST)"
        elif s.battery < rcfg.battery_min:
            rejections[serial] = f"batería {s.battery:.1f}% < mínimo {rcfg.battery_min:.0f}%"
        elif s.busy:
            rejections[serial] = "ocupado"
        else:
            candidates.append(s)

    # Ranking: el que ya lleva la herramienta pedida primero (cambiarla lleva
    # su tiempo); después, más batería; alfabético en empate. Sustituir la
    # batería por "menor distancia al destino" (usar `position`) cuando haga falta.
    candidates.sort(key=lambda c: (bool(tool) and c.tool != tool, -c.battery, c.serial))

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
                        rejections=rejections, ranking=[c.serial for c in candidates])
