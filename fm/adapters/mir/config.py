"""Configuración propia del driver MiR: lo que hay en `robots.<serial>` que el
core no interpreta, fusionado con los defaults de `drivers.mir`.

```yaml
drivers:
  mir:
    mission_group: "mirur-tknika"          # opcional: solo se indexan estas missions
    charge_mission: "Carga en estación"    # opcional: auto-carga (H4)
    tool_register: 5                       # registro PLC con la herramienta montada (decisión 184);
                                           # con catálogo `tools:`, por defecto el 5 (decisión 192)
    # positions_allowlist: [H2D1-VL]       # opcional: positions admitidas como parámetro
robots:
  mir-1:
    host: 192.168.15.5                     # o env MIR_HOST_MIR_1
    actions:
      ir_a: { mission: "Ir a posición", position_inputs: [target_pos], required_inputs: [pieza] }
      pedir: { mission: "Pedir piezas", number_inputs: [n_piezas_pedidas] }
      llevar:
        mission: "Llevar piezas"
        position_inputs: [target_pos]
        positions_allowlist: [Deshecho, Montaje]          # solo estas para ESTA action
        number_inputs:                                    # forma con rango (deslizador en la UI)
          n_piezas_pedidas: { min: 1, max: 10, step: 1 }
```

`auth` sale del entorno: `MIR_AUTH_<SERIAL>`, si no `MIR_AUTH` (alias
`AUTH_HEADER`, nombre del proyecto anterior).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping

from fm.config import ConfigError, env_key

log = logging.getLogger("fm.mir")


@dataclass
class NumberRange:
    """Límites de un `number_inputs`. None = sin límite por ese lado."""
    min: float | None = None
    max: float | None = None
    step: float | None = None


@dataclass
class ActionConfig:
    """Cómo se traduce un `actionType` VDA a una mission del MiR."""
    action_type: str
    mission: str                                  # nombre de la mission en la web del MiR
    position_inputs: list[str] = field(default_factory=list)   # inputs cuyo valor es un nombre de position
    required_inputs: list[str] = field(default_factory=list)   # inputs obligatorios (se reenvían tal cual)
    number_inputs: list[str] = field(default_factory=list)     # inputs obligatorios numéricos ("5" → 5)
    number_ranges: dict[str, NumberRange] = field(default_factory=dict)   # límites opcionales por input
    positions_allowlist: set[str] | None = None   # None = las que admita el robot
    tool_input: str | None = None                 # input con la herramienta pedida (va su código, decisión 184)


@dataclass
class MirRobotConfig:
    serial: str
    host: str
    auth: str                                     # cabecera Authorization completa ("Basic ...")
    actions: dict[str, ActionConfig]
    mission_group: str | None = None              # None = todas las missions del robot
    charge_mission: str | None = None             # None = auto-carga desactivada
    positions_allowlist: set[str] | None = None   # None = cualquier position del robot
    # Herramientas (decisión 184): el registro PLC donde el programa del
    # brazo escribe el código de la que lleva (None = no se lee) y el
    # catálogo `tools:` nombre → código, que pone el core.
    tool_register: int | None = None
    tool_codes: dict[str, int] = field(default_factory=dict)
    # Herramienta fija (decisión 193): un brazo sin cambiador cuyo programa no
    # escribe el registro (mir-1: UR5 con la DH140). Con ella no se lee nada.
    fixed_tool: str | None = None


def _parse_numbers(serial: str, atype: str, raw) -> dict:
    """`number_inputs` admite dos formas: lista de nombres (sin límites) o
    mapping nombre → `{min, max, step}` (cada clave opcional)."""
    if not raw:
        return {}
    if not isinstance(raw, Mapping):
        return {"number_inputs": [str(k) for k in raw]}
    ranges: dict[str, NumberRange] = {}
    for key, spec in raw.items():
        spec = spec or {}
        try:
            r = NumberRange(*(None if spec.get(k) is None else float(spec[k]) for k in ("min", "max", "step")))
        except (TypeError, ValueError, AttributeError):
            raise ConfigError(f"[{serial}] actions.{atype}.number_inputs.{key}: min/max/step deben ser números") from None
        if r.min is not None and r.max is not None and r.min > r.max:
            raise ConfigError(f"[{serial}] actions.{atype}.number_inputs.{key}: min > max")
        if r.step is not None and r.step <= 0:
            raise ConfigError(f"[{serial}] actions.{atype}.number_inputs.{key}: step debe ser > 0")
        ranges[str(key)] = r
    return {"number_inputs": list(ranges), "number_ranges": ranges}


def parse_config(serial: str, raw: Mapping, defaults: Mapping, env: Mapping[str, str]) -> MirRobotConfig:
    """`robots.<serial>` (crudo) + `drivers.mir` + entorno → `MirRobotConfig`.
    Lanza `ConfigError` si falta algo imprescindible."""
    merged = {**defaults, **raw}
    key = env_key(serial)
    host = env.get(f"MIR_HOST_{key}") or merged.get("host")
    if not host:
        raise ConfigError(f"[{serial}] falta 'host' (o la variable MIR_HOST_{key})")
    auth = env.get(f"MIR_AUTH_{key}") or env.get("MIR_AUTH") or env.get("AUTH_HEADER", "")
    if not auth:
        log.warning("[%s] sin token MIR_AUTH: las llamadas REST fallarán", serial)

    actions: dict[str, ActionConfig] = {}
    for atype, a in (merged.get("actions") or {}).items():
        if not isinstance(a, Mapping) or not a.get("mission"):
            raise ConfigError(f"[{serial}] actions.{atype} necesita 'mission' (nombre en la web del MiR)")
        actions[atype] = ActionConfig(
            action_type=atype,
            mission=str(a["mission"]),
            position_inputs=list(a.get("position_inputs") or []),
            required_inputs=list(a.get("required_inputs") or []),
            **_parse_numbers(serial, atype, a.get("number_inputs")),
            positions_allowlist=set(a["positions_allowlist"]) if a.get("positions_allowlist") else None,
            tool_input=str(a["tool_input"]) if a.get("tool_input") else None,
        )

    allow = merged.get("positions_allowlist")
    # Con catálogo `tools:` se lee el registro 5 salvo que se diga otro: el
    # `D2M_Tool_tengo` del protocolo MiR ↔ brazo del otro proyecto (decisión
    # 192; ahí el 1 es el comando y el 2 la herramienta que quiere el MiR).
    # Sin catálogo, ninguno.
    reg = merged.get("tool_register", 5 if merged.get("tool_codes") else None)
    # Registros PLC del MiR: 1–200 (1–100 enteros, 101–200 decimales).
    if reg is not None and (isinstance(reg, bool) or not isinstance(reg, int) or not 1 <= reg <= 200):
        raise ConfigError(f"[{serial}] tool_register debe ser un registro PLC del MiR (1–200); hay {reg!r}")
    # `tool:` en el robot = la lleva siempre (decisión 193): no se lee el registro.
    fixed = merged.get("tool")
    if fixed is not None:
        fixed = str(fixed)
        if fixed not in (merged.get("tool_codes") or {}):
            raise ConfigError(f"[{serial}] tool: {fixed!r} no está en el catálogo 'tools:' "
                              f"(hay {sorted(merged.get('tool_codes') or {})})")
        if merged.get("tool_changer"):
            raise ConfigError(f"[{serial}] tool (herramienta fija) y tool_changer a la vez: con cambiador la "
                              "herramienta cambia y se lee del registro; quita 'tool'")
        reg = None
    return MirRobotConfig(
        serial=serial,
        host=str(host),
        auth=str(auth),
        actions=actions,
        mission_group=merged.get("mission_group") or None,
        charge_mission=merged.get("charge_mission") or None,
        positions_allowlist=set(allow) if allow else None,
        tool_register=reg,
        tool_codes=dict(merged.get("tool_codes") or {}),
        fixed_tool=fixed,
    )
