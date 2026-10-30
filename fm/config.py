"""Carga de configuración: `.env` (secretos y red) + `config/fleet.yaml` (flota).

El core solo entiende las claves genéricas (driver, manufacturer, battery_min,
qué actionTypes soporta cada robot, auto-carga, asignador, MQTT). El resto
del bloque de cada robot (`host`, `actions.<tipo>.mission`, ...) se entrega
crudo al driver de su marca, que lo valida en `fm/adapters/<marca>/config.py`.
Así los errores de configuración saltan al arrancar, no en runtime, y el core
no sabe qué claves necesita cada marca.

Las actions se definen UNA vez en el catálogo `actions:` de la raíz y cada
robot lista las que tiene habilitadas (decisiones 65 y 92): con N robots no
se puede repetir cada definición en cada robot, ni pintarla en la UI. El
core no interpreta las definiciones; solo copia la de cada action habilitada
en `raw["actions"]`, así que el driver recibe el mismo dict de siempre.
La excepción es `expected_s` (decisión 123): tiempo normal de ejecución de
la action, genérico para cualquier marca; el core lo usa para avisar cuando
una order tarda más (`OrderTracker.watch_duration`).

`idle_return` (decisión 136): qué action lanza el FM por su cuenta cuando un
robot lleva un rato parado (ir a la zona de espera). Se define para la flota
y cada robot puede usar otra o desactivarla; la action tiene que estar
habilitada en el robot, porque la traduce su driver como la de una order.
Con `zone` (decisión 138) solo se manda a un robot que esté fuera de esa zona.

`queue` (decisión 156): límites de la cola de `fleet/order` (`fm/tasks.py`):
cuántas tareas pueden esperar y cuánto tiempo como mucho.
"""
from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

log = logging.getLogger("fm.config")
ROOT = Path(__file__).resolve().parents[1]

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
class IdleReturnConfig:
    """Vuelta a la espera (decisión 136): tras `after_s` segundos parado, el
    FM lanza la action `action` (del catálogo, con `params`). Sin action,
    desactivada. Con `zone` (GUID o nombre de una zona del mapa, decisión
    138), solo si el robot está fuera de ella."""
    action: str | None = None
    after_s: float = 30.0
    params: dict[str, Any] = field(default_factory=dict)
    zone: str | None = None

    @property
    def enabled(self) -> bool:
        return self.action is not None


@dataclass
class RobotConfig:
    """Parte genérica de `robots.<serial>`. `raw` es el bloque completo, para el driver."""
    serial: str
    driver: str = DEFAULT_DRIVER
    manufacturer: str | None = None               # None = el que declare el driver
    battery_min: float = 25.0                     # % mínimo para aceptar orders
    action_types: frozenset[str] = frozenset()    # actions habilitadas (lista `robots.<serial>.actions`)
    expected_s: dict[str, float] = field(default_factory=dict)   # actionType → s normales en ejecución
    raw: dict = field(default_factory=dict)       # con `actions` ya expandido: actionType → definición
    idle_return: IdleReturnConfig = field(default_factory=IdleReturnConfig)   # la que vale para ESTE robot


@dataclass
class AutoChargeConfig:
    """Umbrales de la auto-carga (H4). Qué job es "cargar" lo decide cada driver."""
    battery_floor: float = 20.0
    priority: int = 10
    abort_cooldown_s: float = 60.0


@dataclass
class QueueConfig:
    """Cola de `fleet/order` (decisión 156). Con `max_size` tareas esperando,
    la siguiente se rechaza: si la cola se llena, algo va mal aguas arriba y
    es mejor que se note. `max_wait_s` = None: una tarea espera lo que haga
    falta; con un número, al pasar ese tiempo sin robot sale como EXPIRED."""
    max_size: int = 50
    max_wait_s: float | None = None
    # Días que se guardan las terminadas en el histórico (D8, decisión 160);
    # None = no se borran. Se purga al arrancar el FM.
    keep_days: float | None = 30.0


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    # Segmento <manufacturer> de los topics `fleet/*`. Cada robot publica bajo
    # el manufacturer de su driver (norma §6.2); `fleet` no es un robot, así
    # que lleva un nombre de flota propio (decisión 30).
    fleet_manufacturer: str = "imperial_fleet"


@dataclass
class FleetConfig:
    robots: dict[str, RobotConfig]
    auto_charge: AutoChargeConfig = field(default_factory=AutoChargeConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    drivers: dict[str, dict] = field(default_factory=dict)   # defaults por marca: bloque `drivers:`
    actions: dict[str, dict] = field(default_factory=dict)   # catálogo `actions:`, tal cual (lo valida el driver)
    prefer_not_charging: bool = True
    idle_return: IdleReturnConfig = field(default_factory=IdleReturnConfig)   # la de la flota (bloque raíz)
    queue: QueueConfig = field(default_factory=QueueConfig)
    # Site de la flota (decisión 129): solo trabajan los robots cuyo mapa
    # activo es de este site. None = sin comprobar (como antes).
    site: str | None = None

    def driver_defaults(self, name: str) -> dict:
        return dict(self.drivers.get(name) or {})


def _site(value) -> str | None:
    """`site:` de la raíz: el GUID del site de la flota, o nada."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError("'site' debe ser el GUID de un site (texto)")
    return value.strip()


def env_key(serial: str) -> str:
    """`mir-2` → `MIR_2`, para variables tipo MIR_AUTH_MIR_2."""
    return serial.upper().replace("-", "_").replace(".", "_")


def install_paths() -> tuple[Path, Path]:
    """`fleet.yaml` y `catalog/` de la instalación (decisión 135). Son los
    datos de UNA fábrica o un centro (sus robots, sus positions, sus
    missions) y no van en el repo del FM, que es público: `.env` dice dónde
    están con `FM_CONFIG` y `FM_CATALOG` (relativas, a la raíz del repo).
    Sin ellas, `config/fleet.yaml` y `catalog/` del propio repo, fuera de
    git. Llamar después de `load_dotenv`."""
    def at(var: str, default: str) -> Path:
        p = Path(os.environ.get(var) or default)
        return p if p.is_absolute() else ROOT / p
    return at("FM_CONFIG", "config/fleet.yaml"), at("FM_CATALOG", "catalog")


def data_dir() -> Path:
    """Carpeta de los datos que el FM escribe mientras funciona (decisión
    160): hoy, la cola de `fleet/order` en SQLite. No es configuración de la
    instalación (eso es `install_paths`) ni va en git: `FM_DATA` en `.env`,
    relativa a la raíz del repo; por defecto `data/`. Llamar después de
    `load_dotenv`."""
    p = Path(os.environ.get("FM_DATA") or "data")
    return p if p.is_absolute() else ROOT / p


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

    catalog = _parse_catalog(raw.get("actions"))
    fleet_idle = _idle_return("idle_return", raw.get("idle_return"), IdleReturnConfig())
    robots: dict[str, RobotConfig] = {}
    for serial, r in (raw.get("robots") or {}).items():
        r = r or {}
        enabled = _enabled_actions(serial, r.get("actions"), catalog)
        idle = _robot_idle_return(serial, r, enabled, fleet_idle)
        robots[serial] = RobotConfig(
            serial=serial,
            driver=str(r.get("driver", DEFAULT_DRIVER)),
            manufacturer=r.get("manufacturer"),
            battery_min=float(r.get("battery_min", 25)),
            action_types=frozenset(enabled),
            expected_s={a: float(catalog[a]["expected_s"]) for a in enabled if "expected_s" in catalog[a]},
            # Copia profunda: cada driver recibe su propia definición y no
            # puede alterar la de otro robot ni el catálogo.
            raw={**r, "actions": {a: copy.deepcopy(catalog[a]) for a in enabled}},
            idle_return=idle,
        )

    auto_charge = AutoChargeConfig(
        battery_floor=float(ac.get("battery_floor", 20)),
        priority=int(ac.get("priority", 10)),
        abort_cooldown_s=float(ac.get("abort_cooldown_s", 60)),
    )

    mq = raw.get("mqtt") or {}
    if "manufacturer" in mq:
        raise ConfigError("'mqtt.manufacturer' ya no existe: cada robot publica bajo el manufacturer "
                          "de su driver (o 'robots.<serial>.manufacturer'); para fleet/* usa "
                          "'mqtt.fleet_manufacturer'")
    mqtt = MqttConfig(
        host=os.environ.get("MQTT_HOST", mq.get("host", "localhost")),
        port=int(os.environ.get("MQTT_PORT", mq.get("port", 1883))),
        fleet_manufacturer=str(mq.get("fleet_manufacturer", "imperial_fleet")),
    )

    cfg = FleetConfig(
        robots=robots,
        auto_charge=auto_charge,
        mqtt=mqtt,
        drivers={k: (v or {}) for k, v in (raw.get("drivers") or {}).items()},
        actions=catalog,
        prefer_not_charging=bool((raw.get("assigner") or {}).get("prefer_not_charging", True)),
        idle_return=fleet_idle,
        queue=_queue(raw.get("queue")),
        site=_site(raw.get("site")),
    )

    # Histéresis: si battery_min <= battery_floor el robot oscilaría entre
    # "cargar" y "aceptar trabajo". Solo avisamos: es decisión del usuario.
    for r in robots.values():
        if r.battery_min <= auto_charge.battery_floor:
            log.warning("[%s] battery_min (%.0f%%) <= battery_floor (%.0f%%): sin histéresis",
                        r.serial, r.battery_min, auto_charge.battery_floor)
    return cfg


IDLE_KEYS = {"action", "after_s", "params", "zone"}
QUEUE_KEYS = {"max_size", "max_wait_s", "keep_days"}


def _queue(raw) -> QueueConfig:
    """Bloque `queue:` de la raíz; lo que no dice, por defecto."""
    if raw is None:
        return QueueConfig()
    if not isinstance(raw, dict):
        raise ConfigError("queue: se espera {max_size: ..., max_wait_s: ...}")
    unknown = set(raw) - QUEUE_KEYS
    if unknown:
        raise ConfigError(f"queue: claves desconocidas {sorted(unknown)} (valen {sorted(QUEUE_KEYS)})")
    size = raw.get("max_size", QueueConfig.max_size)
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ConfigError("queue.max_size: cuántas tareas pueden esperar (un entero, 1 o más)")
    wait = raw.get("max_wait_s")
    if wait is not None and (isinstance(wait, bool) or not isinstance(wait, (int, float)) or not wait > 0):
        raise ConfigError("queue.max_wait_s: segundos que una tarea puede esperar robot "
                          "(un número mayor que 0), o nada para esperar siempre")
    keep = raw.get("keep_days", QueueConfig.keep_days)
    if keep is not None and (isinstance(keep, bool) or not isinstance(keep, (int, float)) or not keep > 0):
        raise ConfigError("queue.keep_days: días que se guardan las tareas terminadas "
                          "(un número mayor que 0), o nada para no borrarlas")
    return QueueConfig(size, float(wait) if wait is not None else None,
                       float(keep) if keep is not None else None)


def _idle_return(where: str, raw, base: IdleReturnConfig) -> IdleReturnConfig:
    """Bloque `idle_return` (de la flota o de un robot) sobre `base`: lo que
    no dice se hereda. Si cambia de action, sus parámetros y su zona no se
    heredan (serían los de otra action, que lleva a otro sitio)."""
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: se espera {{action: ..., after_s: ..., params: {{...}}}} o false")
    unknown = set(raw) - IDLE_KEYS
    if unknown:
        raise ConfigError(f"{where}: claves desconocidas {sorted(unknown)} (valen {sorted(IDLE_KEYS)})")
    action = raw.get("action", base.action)
    if action is not None and (not isinstance(action, str) or not action.strip()):
        raise ConfigError(f"{where}.action: el nombre de una action del catálogo")
    after = raw.get("after_s", base.after_s)
    if isinstance(after, bool) or not isinstance(after, (int, float)) or not after > 0:
        raise ConfigError(f"{where}.after_s: segundos parado antes de lanzarla (un número mayor que 0)")
    params = raw.get("params", base.params if action == base.action else {}) or {}
    if not isinstance(params, dict) or not all(isinstance(k, str) for k in params):
        raise ConfigError(f"{where}.params: un mapping parámetro → valor (p. ej. {{target_pos: Espera}})")
    zone = raw["zone"] if "zone" in raw else base.zone if action == base.action else None
    if zone is not None and (not isinstance(zone, str) or not zone.strip()):
        raise ConfigError(f"{where}.zone: el GUID (o el nombre) de una zona del mapa, o nada")
    return IdleReturnConfig(action.strip() if action else None, float(after), dict(params),
                            zone.strip() if zone else None)


def _robot_idle_return(serial: str, r: dict, enabled: list[str], fleet: IdleReturnConfig) -> IdleReturnConfig:
    """La vuelta a la espera de un robot: la de la flota, la suya
    (`idle_return: {...}`) o ninguna (`idle_return: false`)."""
    raw = r.get("idle_return", None)
    if raw is False:
        return IdleReturnConfig()
    own = "idle_return" in r and raw is not None
    idle = _idle_return(f"robots.{serial}.idle_return", raw, fleet)
    if idle.enabled and idle.action not in enabled:
        if own:
            raise ConfigError(f"robots.{serial}.idle_return: la action '{idle.action}' no está habilitada "
                              f"en {serial} (añádela a su lista actions)")
        # La de la flota y un robot que no la tiene (p. ej. uno simulado): ese no vuelve.
        log.warning("[%s] no tiene la action '%s' de idle_return: no volverá a la espera", serial, idle.action)
        return IdleReturnConfig()
    return idle


def _parse_catalog(raw) -> dict[str, dict]:
    """Bloque `actions:` de la raíz → `{actionType: definición}`. Solo se
    comprueba la forma; el contenido (`mission`, `position_inputs`…) es de
    cada marca y lo valida su driver."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("'actions' (raíz) debe ser un mapping actionType → definición")
    catalog: dict[str, dict] = {}
    for name, spec in raw.items():
        spec = {} if spec is None else spec          # `coger:` sin nada = `coger: {}`
        if not isinstance(spec, dict):
            raise ConfigError(f"actions.{name}: la definición debe ser un mapping (p.ej. {{mission: ...}})")
        if "expected_s" in spec:
            v = spec["expected_s"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0:
                raise ConfigError(f"actions.{name}.expected_s: debe ser un número de segundos > 0 (hay {v!r})")
        catalog[str(name)] = spec
    return catalog


def _enabled_actions(serial: str, raw, catalog: dict[str, dict]) -> list[str]:
    """`robots.<serial>.actions` → lista de actionTypes del catálogo."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        # Formato anterior al 2026-10-14: cada robot repetía las definiciones.
        # Se rechaza con la pista en vez de convivir con dos formatos (decisión 92).
        raise ConfigError(
            f"robots.{serial}.actions ya no lleva definiciones: muévelas al bloque 'actions:' "
            f"de la raíz (una vez para toda la flota) y deja aquí la lista de las habilitadas, "
            f"p.ej. actions: [{', '.join(map(str, list(raw)[:2]))}]")
    if not isinstance(raw, list):
        raise ConfigError(f"robots.{serial}.actions debe ser una lista de actionTypes")
    names = [str(a) for a in raw]
    dup = sorted({a for a in names if names.count(a) > 1})
    if dup:
        raise ConfigError(f"robots.{serial}.actions: repetidas {dup}")
    unknown = [a for a in names if a not in catalog]
    if unknown:
        raise ConfigError(f"robots.{serial}.actions: {unknown} no están en el catálogo 'actions:' "
                          f"(hay {sorted(catalog)})")
    return names
