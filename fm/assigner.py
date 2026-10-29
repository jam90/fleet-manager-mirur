"""Asignador de flota: función pura `assign(order_action_type, snapshots, cfg)`.

Filtros en orden (CLAUDE.md §6): soporta el actionType → batería ≥ battery_min
→ no ocupado → (opcional) no cargando si hay otro libre. Ranking: mayor
batería, desempate alfabético. Sin candidatos → rechazo con el detalle por robot.

Devuelve el ranking completo, no solo el mejor: si el elegido no puede
traducir la order (p.ej. le falta la mission), el `Dispatcher` prueba el
siguiente (decisión 89). La función sigue sin saber nada de missions.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fm.config import FleetConfig
from fm.vda5050.state import E_NO_MOBILE_ROBOT_AVAILABLE


@dataclass(frozen=True)
class RobotSnapshot:
    serial: str
    battery: float
    busy: bool
    available: bool = True            # False = MANUAL / Error / EmergencyStop / sin REST
    charging: bool = False
    position: tuple[float, float] | None = None   # hueco para "menor distancia" (futuro)
    reason: str = ""                  # por qué no está disponible, si se sabe (p. ej. fuera del site)


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


def assign(action_type: str, snapshots: dict[str, RobotSnapshot], cfg: FleetConfig) -> AssignResult:
    rejections: dict[str, str] = {}
    candidates: list[RobotSnapshot] = []
    for serial in sorted(snapshots):
        s = snapshots[serial]
        rcfg = cfg.robots.get(serial)
        if rcfg is None or action_type not in rcfg.action_types:
            rejections[serial] = f"no soporta '{action_type}'"
        elif not s.available:
            rejections[serial] = s.reason or "no disponible (manual/error/sin REST)"
        elif s.battery < rcfg.battery_min:
            rejections[serial] = f"batería {s.battery:.1f}% < mínimo {rcfg.battery_min:.0f}%"
        elif s.busy:
            rejections[serial] = "ocupado"
        else:
            candidates.append(s)

    # Ranking: más batería primero; alfabético en empate. Sustituir aquí por
    # "menor distancia al destino" (usar `position`) cuando haga falta.
    candidates.sort(key=lambda c: (-c.battery, c.serial))

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
        return AssignResult(None, f"ningún robot puede atender la order: {detail}",
                            E_NO_MOBILE_ROBOT_AVAILABLE, rejections)

    best = candidates[0]
    return AssignResult(best.serial, f"asignado a {best.serial} ({best.battery:.1f}%)",
                        rejections=rejections, ranking=[c.serial for c in candidates])
