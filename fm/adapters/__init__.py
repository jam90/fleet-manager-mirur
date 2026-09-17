"""Registro de drivers: nombre en `robots.<serial>.driver` → fábrica.

Añadir una marca = crear `fm/adapters/<marca>/` con una función
`make_driver(serial, raw, defaults, env) -> RobotDriver` y darla de alta aquí.
Los imports son perezosos para que el core no cargue ninguna marca que no use.
"""
from __future__ import annotations

import os
from importlib import import_module
from typing import TYPE_CHECKING, Mapping

from fm.config import ConfigError

if TYPE_CHECKING:
    from fm.adapters.base import RobotDriver
    from fm.config import FleetConfig, RobotConfig

# nombre del driver → módulo que expone `make_driver(serial, raw, defaults, env)`
DRIVERS: dict[str, str] = {
    "mir": "fm.adapters.mir",
    "sim": "fm.adapters.sim",
}


def make_driver(rcfg: RobotConfig, fleet: FleetConfig,
                env: Mapping[str, str] | None = None) -> RobotDriver:
    """Construye el driver de un robot según `rcfg.driver`. Lanza `ConfigError`
    si el driver no existe o su configuración no es válida."""
    module = DRIVERS.get(rcfg.driver)
    if module is None:
        raise ConfigError(f"[{rcfg.serial}] driver '{rcfg.driver}' desconocido; hay: {sorted(DRIVERS)}")
    factory = import_module(module).make_driver
    driver = factory(rcfg.serial, rcfg.raw, fleet.driver_defaults(rcfg.driver),
                     os.environ if env is None else env)
    if rcfg.manufacturer:
        driver.manufacturer = rcfg.manufacturer   # `robots.<s>.manufacturer` manda sobre el del driver
    return driver
