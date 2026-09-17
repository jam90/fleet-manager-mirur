"""Utilidades compartidas por los scripts: argumentos, logging y acceso al robot."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Los scripts se lanzan desde la raíz del repo o desde scripts/: en ambos
# casos queremos importar `fm` de la raíz.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fm.config import FleetConfig, RobotConfig, load_config  # noqa: E402
from fm.mir_client import MirClient  # noqa: E402


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(message)s")


def load() -> FleetConfig:
    return load_config(ROOT / "config" / "fleet.yaml", ROOT / ".env")


def robot_or_exit(cfg: FleetConfig, serial: str) -> RobotConfig:
    if serial not in cfg.robots:
        print(f"robot '{serial}' no está en fleet.yaml; disponibles: {sorted(cfg.robots)}")
        sys.exit(2)
    return cfg.robots[serial]


def client_for(robot: RobotConfig) -> MirClient:
    return MirClient(robot.host, robot.auth)
