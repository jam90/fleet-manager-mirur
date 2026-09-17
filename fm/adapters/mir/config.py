"""Configuración propia del driver MiR: lo que hay en `robots.<serial>` que el
core no interpreta, fusionado con los defaults de `drivers.mir`.

```yaml
drivers:
  mir:
    mission_group: "mirur-tknika"          # opcional: solo se indexan estas missions
    charge_mission: "Carga en estación"    # opcional: auto-carga (H4)
    # positions_allowlist: [H2D1-VL]       # opcional: positions admitidas como parámetro
robots:
  mir-1:
    host: 192.168.15.5                     # o env MIR_HOST_MIR_1
    actions:
      ir_a: { mission: "Ir a posición", position_inputs: [target_pos], required_inputs: [pieza] }
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
class ActionConfig:
    """Cómo se traduce un `actionType` VDA a una mission del MiR."""
    action_type: str
    mission: str                                  # nombre de la mission en la web del MiR
    position_inputs: list[str] = field(default_factory=list)   # inputs cuyo valor es un nombre de position
    required_inputs: list[str] = field(default_factory=list)   # inputs obligatorios (se reenvían tal cual)


@dataclass
class MirRobotConfig:
    serial: str
    host: str
    auth: str                                     # cabecera Authorization completa ("Basic ...")
    actions: dict[str, ActionConfig]
    mission_group: str | None = None              # None = todas las missions del robot
    charge_mission: str | None = None             # None = auto-carga desactivada
    positions_allowlist: set[str] | None = None   # None = cualquier position del robot


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
        )

    allow = merged.get("positions_allowlist")
    return MirRobotConfig(
        serial=serial,
        host=str(host),
        auth=str(auth),
        actions=actions,
        mission_group=merged.get("mission_group") or None,
        charge_mission=merged.get("charge_mission") or None,
        positions_allowlist=set(allow) if allow else None,
    )
