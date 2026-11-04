"""Modo mantenimiento por robot: dónde se guarda (decisiones 167–170).

Un robot en mantenimiento sigue en `fleet.yaml` y sigue publicando su
telemetría, pero el FM no le manda nada: ni orders (de la flota ni
dirigidas), ni auto-carga, ni vuelta a la espera. Lo que hace cada parte
está en `fm/robot.py`; aquí solo se guarda **qué robots están en
mantenimiento y desde cuándo**.

Es estado de operación, no configuración: va en el mismo SQLite que la cola
de `fleet/order` (`data/<flota>.sqlite`), no en `fleet.yaml`. Así sobrevive
a reiniciar el FM (alguien está calibrando el robot y no debe recibir
trabajo porque el FM se haya reiniciado) sin tocar el repo de la
instalación cada vez.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:   # solo para tipos: robot.py importa este módulo
    from fm.robot import Robot

log = logging.getLogger("fm.maintenance")


class MaintenanceStore(Protocol):
    def load(self) -> dict[str, float]:
        """serial → epoch en que se pidió el mantenimiento."""
        ...

    def set(self, serial: str, since: float | None) -> None:
        """Guarda que `serial` está en mantenimiento desde `since`; None = ya no."""
        ...


class MemoryMaintenanceStore:
    """En RAM: para los tests y para un FM sin fichero de datos."""

    def __init__(self, initial: dict[str, float] | None = None):
        self._rows = dict(initial or {})

    def load(self) -> dict[str, float]:
        return dict(self._rows)

    def set(self, serial: str, since: float | None) -> None:
        if since is None:
            self._rows.pop(serial, None)
        else:
            self._rows[serial] = since


SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance (
    serial TEXT PRIMARY KEY,   -- robot (nombre lógico de fleet.yaml)
    since  REAL NOT NULL       -- epoch UTC en que se pidió
);
"""


class SqliteMaintenanceStore:
    """Tabla `maintenance` del SQLite de la flota. Conexión propia (la cola
    tiene la suya); las dos escriben solo desde el hilo principal, y cada
    cambio es una transacción, como en la cola (decisión 160)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)

    def load(self) -> dict[str, float]:
        return {serial: since for serial, since in self.db.execute("SELECT serial, since FROM maintenance")}

    def set(self, serial: str, since: float | None) -> None:
        if since is None:
            self.db.execute("DELETE FROM maintenance WHERE serial = ?", (serial,))
        else:
            self.db.execute("INSERT OR REPLACE INTO maintenance (serial, since) VALUES (?, ?)", (serial, since))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


def attach(robots: dict[str, "Robot"], store: MaintenanceStore) -> None:
    """Da a cada robot el almacén y el mantenimiento que tenía al pararse el
    FM. Va ANTES del primer `poll()`: si no, un robot en mantenimiento con
    la batería baja recibiría una carga nada más arrancar. Las filas de un
    robot que ya no está en la flota se dejan (si vuelve, vuelve en
    mantenimiento)."""
    saved = store.load()
    for serial, robot in robots.items():
        robot.maintenance_store = store
        robot.maintenance_since = saved.get(serial)
        if robot.in_maintenance:
            log.info("[%s] sigue en mantenimiento (de antes del arranque)", serial)
