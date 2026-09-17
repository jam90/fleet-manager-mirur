"""Driver simulado: un robot sin hardware que "ejecuta" cualquier actionType.

Sirve para probar el core end-to-end sin robots, como material didáctico
(`fleet.yaml` con un MiR real y un `sim-1`) y como prueba de que la frontera
`RobotDriver` no está pensada solo para MiR: no tiene missions ni GUIDs.
"""
from __future__ import annotations

from typing import Mapping

from fm.adapters.sim.driver import SimDriver, SimRobotConfig, parse_config  # noqa: F401


def make_driver(serial: str, raw: Mapping, defaults: Mapping, env: Mapping[str, str]) -> SimDriver:
    return SimDriver(parse_config(serial, raw, defaults))
