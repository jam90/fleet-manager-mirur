"""Comprobación periódica de qué tiene cada robot del catálogo (decisión 86).

Cada `period` segundos (5 min) lanza la comprobación **rápida** en cada robot
que ofrece sincronización y responde; la **completa** solo bajo demanda. El
resultado (un informe por robot, con su hora) se guarda en memoria y se
emite con `publish(serial, informe)`: la web lo reenvía por el WebSocket.

Corre en su propio hilo, nunca en el bucle de 1 Hz. No sabe de MiR: usa el
`check(deep)` del objeto que da `driver.make_sync()`. Si ese objeto está
ocupado (un despliegue en curso tiene su `lock`), el turno periódico se salta.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger("fm.web")


class SyncMonitor:
    def __init__(self, syncs: dict, robots: dict, publish: Callable[[str, dict], None],
                 period: float = 300.0, first_delay: float = 15.0):
        self.syncs, self.robots, self.publish = syncs, robots, publish
        self.period, self.first_delay = period, first_delay
        self.reports: dict[str, dict] = {}
        self.running: set[str] = set()          # comprobaciones en curso
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def online(self, serial: str) -> bool:
        return self.robots[serial].last_telemetry is not None

    def check(self, serial: str, deep: bool = False, wait_lock: bool = False) -> bool:
        """Comprueba un robot en el hilo actual. False si ya había una en curso,
        o si el robot está ocupado sincronizando y `wait_lock` es False."""
        sync = self.syncs[serial]
        with self._lock:
            if serial in self.running:
                return False
            self.running.add(serial)
        try:
            if not wait_lock and sync.lock.locked():
                return False
            try:
                report = sync.check(deep=deep)
            except Exception as e:              # robot inalcanzable, 5xx…
                log.warning("[%s] comprobación del catálogo fallida: %s", serial, e)
                report = {**self.reports.get(serial, {}), "error": str(e)}
            else:
                report["error"] = None
            self.reports[serial] = report
            self.publish(serial, {"report": report})
            return True
        finally:
            with self._lock:
                self.running.discard(serial)

    def check_async(self, serial: str, deep: bool = False) -> bool:
        if serial in self.running:
            return False
        threading.Thread(target=self.check, args=(serial, deep, True),
                         name=f"check-{serial}", daemon=True).start()
        return True

    def _loop(self) -> None:
        if self._stop.wait(self.first_delay):   # que los robots den su primer poll
            return
        while True:
            for serial in self.syncs:
                if self._stop.is_set():
                    return
                if self.online(serial):
                    self.check(serial)
            if self._stop.wait(self.period):
                return

    def start(self) -> None:
        if self.syncs:
            threading.Thread(target=self._loop, name="sync-monitor", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
