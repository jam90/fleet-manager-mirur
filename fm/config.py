"""Carga de configuración: `.env` (secretos y red) + `config/fleet.yaml` (flota).

Se convierte todo a dataclasses para que el resto del código no toque dicts
sueltos y los errores de configuración salten al arrancar, no en runtime.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

log = logging.getLogger("fm.config")


@dataclass
class ActionConfig:
    """Cómo se traduce un `actionType` VDA a una mission del MiR."""
    action_type: str
    mission: str                                  # nombre de la mission en la web del MiR
    position_inputs: list[str] = field(default_factory=list)   # inputs cuyo valor es un nombre de position
    required_inputs: list[str] = field(default_factory=list)   # inputs obligatorios (se reenvían tal cual)


@dataclass
class RobotConfig:
    serial: str
    host: str
    auth: str                                     # cabecera Authorization completa ("Basic ...")
    battery_min: float                            # % mínimo para aceptar orders
    actions: dict[str, ActionConfig]


@dataclass
class AutoChargeConfig:
    mission: str | None                           # None = auto-carga desactivada
    battery_floor: float = 20.0
    priority: int = 10
    abort_cooldown_s: float = 60.0


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    manufacturer: str = "MiR"


@dataclass
class FleetConfig:
    robots: dict[str, RobotConfig]
    mission_group: str | None                     # grupo de missions del MiR a indexar (None = todas)
    auto_charge: AutoChargeConfig
    mqtt: MqttConfig
    positions_allowlist: set[str] | None = None   # None = cualquier position del robot
    prefer_not_charging: bool = True


def _env_key(serial: str) -> str:
    """`mir-2` → `MIR_2`, para variables tipo MIR_AUTH_MIR_2."""
    return serial.upper().replace("-", "_").replace(".", "_")


def load_config(yaml_path: str | Path = "config/fleet.yaml",
                env_path: str | Path | None = None) -> FleetConfig:
    load_dotenv(env_path)  # no pisa variables ya definidas en el entorno
    raw = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}

    # AUTH_HEADER es el nombre que usaba el .env del proyecto anterior; se
    # acepta como alias para poder reutilizar ese fichero sin tocarlo.
    default_auth = os.environ.get("MIR_AUTH") or os.environ.get("AUTH_HEADER", "")
    robots: dict[str, RobotConfig] = {}
    for serial, r in (raw.get("robots") or {}).items():
        actions = {}
        for atype, a in (r.get("actions") or {}).items():
            actions[atype] = ActionConfig(
                action_type=atype,
                mission=a["mission"],
                position_inputs=list(a.get("position_inputs") or []),
                required_inputs=list(a.get("required_inputs") or []),
            )
        key = _env_key(serial)
        robots[serial] = RobotConfig(
            serial=serial,
            host=os.environ.get(f"MIR_HOST_{key}", r.get("host", "")),
            auth=os.environ.get(f"MIR_AUTH_{key}", default_auth),
            battery_min=float(r.get("battery_min", 25)),
            actions=actions,
        )
        if not robots[serial].auth:
            log.warning("[%s] sin token MIR_AUTH: las llamadas REST fallarán", serial)

    ac = raw.get("auto_charge") or {}
    auto_charge = AutoChargeConfig(
        mission=ac.get("mission") or None,
        battery_floor=float(ac.get("battery_floor", 20)),
        priority=int(ac.get("priority", 10)),
        abort_cooldown_s=float(ac.get("abort_cooldown_s", 60)),
    )

    mq = raw.get("mqtt") or {}
    mqtt = MqttConfig(
        host=os.environ.get("MQTT_HOST", mq.get("host", "localhost")),
        port=int(os.environ.get("MQTT_PORT", mq.get("port", 1883))),
        manufacturer=str(mq.get("manufacturer", "MiR")),
    )

    allow = raw.get("positions_allowlist")
    cfg = FleetConfig(
        robots=robots,
        mission_group=raw.get("mission_group") or None,
        auto_charge=auto_charge,
        mqtt=mqtt,
        positions_allowlist=set(allow) if allow else None,
        prefer_not_charging=bool((raw.get("assigner") or {}).get("prefer_not_charging", True)),
    )

    # Histéresis: si battery_min <= battery_floor el robot oscilaría entre
    # "cargar" y "aceptar trabajo". Solo avisamos: es decisión del usuario.
    for r in robots.values():
        if r.battery_min <= auto_charge.battery_floor:
            log.warning("[%s] battery_min (%.0f%%) <= battery_floor (%.0f%%): sin histéresis",
                        r.serial, r.battery_min, auto_charge.battery_floor)
    return cfg
