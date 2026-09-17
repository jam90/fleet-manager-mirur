"""Utilidades compartidas por los scripts: argumentos, logging y acceso al robot."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Los scripts se lanzan desde la raíz del repo o desde scripts/: en ambos
# casos queremos importar `fm` de la raíz.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

from fm.adapters.mir.client import MirClient  # noqa: E402
from fm.adapters.mir.config import MirRobotConfig, parse_config  # noqa: E402
from fm.config import FleetConfig, load_config  # noqa: E402


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(message)s")


def load() -> FleetConfig:
    return load_config(ROOT / "config" / "fleet.yaml", ROOT / ".env")


def robot_or_exit(cfg: FleetConfig, serial: str) -> MirRobotConfig:
    """Config MiR del robot (estos scripts son específicos de MiR)."""
    r = cfg.robots.get(serial)
    if r is None or r.driver != "mir":
        mir = sorted(s for s, x in cfg.robots.items() if x.driver == "mir")
        print(f"robot '{serial}' no es un MiR de fleet.yaml; disponibles: {mir}")
        sys.exit(2)
    return parse_config(serial, r.raw, cfg.driver_defaults("mir"), os.environ)


def client_for(robot: MirRobotConfig) -> MirClient:
    return MirClient(robot.host, robot.auth)
