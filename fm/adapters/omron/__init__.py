"""Driver OMRON (LD/HD/MD) por ARCL. Plan y lo visto en el robot real:
docs/plan-omron.md; robot simulado para probar sin hardware: simuladores/omron/.
"""
from __future__ import annotations

from typing import Mapping

from fm.adapters.omron.driver import OmronDriver, OmronRobotConfig, parse_config  # noqa: F401


def make_driver(serial: str, raw: Mapping, defaults: Mapping, env: Mapping[str, str]) -> OmronDriver:
    return OmronDriver(parse_config(serial, raw, defaults, env))
