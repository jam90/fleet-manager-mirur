"""Driver MiR250 (REST API v2.0.0). Ver `driver.py`."""
from __future__ import annotations

from typing import Mapping

from fm.adapters.mir.config import MirRobotConfig, parse_config  # noqa: F401
from fm.adapters.mir.driver import MirDriver  # noqa: F401


def make_driver(serial: str, raw: Mapping, defaults: Mapping, env: Mapping[str, str]) -> MirDriver:
    return MirDriver(parse_config(serial, raw, defaults, env))
