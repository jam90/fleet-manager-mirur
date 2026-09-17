"""Carga de configuración: `.env` (secretos y red) + `config/fleet.yaml` (flota).

El core solo entiende las claves genéricas (driver, manufacturer, battery_min,
qué actionTypes soporta cada robot, auto-carga, asignador, MQTT). El resto
del bloque de cada robot (`host`, `actions.<tipo>.mission`, ...) se entrega
crudo al driver de su marca, que lo valida en `fm/adapters/<marca>/config.py`.
Así los errores de configuración saltan al arrancar, no en runtime, y el core
no sabe qué claves necesita cada marca.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

log = logging.getLogger("fm.config")

DEFAULT_DRIVER = "mir"

# Claves que vivían en la raíz del fleet.yaml antiguo y ahora son del driver MiR.
# Se rechazan con un mensaje claro en vez de ignorarlas en silencio.
_MOVED_ROOT_KEYS = {
    "mission_group": "drivers.mir.mission_group",
    "positions_allowlist": "drivers.mir.positions_allowlist",
}


class ConfigError(ValueError):
    """fleet.yaml inválido. Se lanza al arrancar con el motivo."""


@dataclass
class RobotConfig:
    """Parte genérica de `robots.<serial>`. `raw` es el bloque completo, para el driver."""
    serial: str
    driver: str = DEFAULT_DRIVER
    manufacturer: str | None = None               # None = el que declare el driver
    battery_min: float = 25.0                     # % mínimo para aceptar orders
    action_types: frozenset[str] = frozenset()    # claves de `actions:` (convención de todos los drivers)
    raw: dict = field(default_factory=dict)


@dataclass
class AutoChargeConfig:
    """Umbrales de la auto-carga (H4). Qué job es "cargar" lo decide cada driver."""
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
    auto_charge: AutoChargeConfig = field(default_factory=AutoChargeConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    drivers: dict[str, dict] = field(default_factory=dict)   # defaults por marca: bloque `drivers:`
    prefer_not_charging: bool = True

    def driver_defaults(self, name: str) -> dict:
        return dict(self.drivers.get(name) or {})


def env_key(serial: str) -> str:
    """`mir-2` → `MIR_2`, para variables tipo MIR_AUTH_MIR_2."""
    return serial.upper().replace("-", "_").replace(".", "_")


def load_config(yaml_path: str | Path = "config/fleet.yaml",
                env_path: str | Path | None = None) -> FleetConfig:
    load_dotenv(env_path)  # no pisa variables ya definidas en el entorno
    raw = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}

    for key, dest in _MOVED_ROOT_KEYS.items():
        if key in raw:
            raise ConfigError(f"'{key}' ya no va en la raíz de fleet.yaml: muévelo a '{dest}'")
    ac = raw.get("auto_charge") or {}
    if "mission" in ac:
        raise ConfigError("'auto_charge.mission' ya no existe: muévelo a 'drivers.mir.charge_mission'")

    robots: dict[str, RobotConfig] = {}
    for serial, r in (raw.get("robots") or {}).items():
        r = r or {}
        robots[serial] = RobotConfig(
            serial=serial,
            driver=str(r.get("driver", DEFAULT_DRIVER)),
            manufacturer=r.get("manufacturer"),
            battery_min=float(r.get("battery_min", 25)),
            action_types=frozenset(r.get("actions") or {}),
            raw=r,
        )

    auto_charge = AutoChargeConfig(
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

    cfg = FleetConfig(
        robots=robots,
        auto_charge=auto_charge,
        mqtt=mqtt,
        drivers={k: (v or {}) for k, v in (raw.get("drivers") or {}).items()},
        prefer_not_charging=bool((raw.get("assigner") or {}).get("prefer_not_charging", True)),
    )

    # Histéresis: si battery_min <= battery_floor el robot oscilaría entre
    # "cargar" y "aceptar trabajo". Solo avisamos: es decisión del usuario.
    for r in robots.values():
        if r.battery_min <= auto_charge.battery_floor:
            log.warning("[%s] battery_min (%.0f%%) <= battery_floor (%.0f%%): sin histéresis",
                        r.serial, r.battery_min, auto_charge.battery_floor)
    return cfg
