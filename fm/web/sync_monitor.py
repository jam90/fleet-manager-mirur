"""Comprobación periódica de qué tiene cada robot del catálogo (decisión 86).

Cada `period` segundos (5 min) lanza la comprobación **rápida** en cada robot
que ofrece sincronización y responde; la **completa** solo bajo demanda. El
resultado (un informe por robot, con su hora) se guarda en memoria y se
emite con `publish(serial, informe)`: la web lo reenvía por el WebSocket.

Corre en su propio hilo, nunca en el bucle de 1 Hz. No sabe de MiR: usa el
`check(deep)` del objeto que da `driver.make_sync()`. Si ese objeto está
ocupado (un despliegue en curso tiene su `lock`), el turno periódico se salta.

La rápida no borra lo que sabía la completa (decisión 186): la rápida solo
dice si el robot lo tiene; si lo tiene y la completa había visto que era
«distinta» o «corregir» (o igual), se conserva ese dato, con la hora de la
completa. El informe dice qué recursos vienen de ella y de cuándo (`deep`). Lo que cambia en el robot o en el catálogo (desplegar, traer,
capturar) se olvida con `forget()` o se da por igual con `same()`: ahí la
completa ya no sabe nada.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("fm.web")


def now_iso() -> str:
    """La hora como la de `checked_at` de los informes."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class SyncMonitor:
    def __init__(self, syncs: dict, robots: dict, publish: Callable[[str, dict], None],
                 period: float = 300.0, first_delay: float = 15.0):
        self.syncs, self.robots, self.publish = syncs, robots, publish
        self.period, self.first_delay = period, first_delay
        self.reports: dict[str, dict] = {}
        self.seq = 0                            # número de informe: distingue dos en el mismo segundo
        self.running: set[str] = set()          # comprobaciones en curso
        self.deep_wanted: set[str] = set()      # completa pedida con otra en curso: va después
        # Lo que sabe la última completa de cada robot: guid → estado, y cuándo.
        self.deep: dict[str, dict[str, tuple[str, str]]] = {}
        # Recurso → de qué robot se trajo al catálogo y qué robots tienen aún la
        # versión anterior (los demás de entonces, hasta que se les despliegue).
        # Así la web no les ofrece «Traer al catálogo», que desharía lo traído.
        self.brought: dict[str, dict] = {}
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
                self._merge(serial, report, deep)
            with self._lock:
                self.seq += 1
                report["seq"] = self.seq
            self.reports[serial] = report
            # Con lo traído al día: una página abierta no ofrece deshacerlo (lo lee de aquí).
            self.publish(serial, {"report": report, "brought": self.brought})
            return True
        finally:
            with self._lock:
                self.running.discard(serial)
                again = serial in self.deep_wanted
                self.deep_wanted.discard(serial)
            if again:
                self.check_async(serial, deep=True)

    def check_async(self, serial: str, deep: bool = False) -> bool:
        """En su hilo. Con otra en curso, una rápida no hace falta (False);
        una completa se hace al acabar esa (True): quien la pidió la espera."""
        with self._lock:
            if serial in self.running:
                if not deep:
                    return False
                self.deep_wanted.add(serial)
                return True
        threading.Thread(target=self.check, args=(serial, deep, True),
                         name=f"check-{serial}", daemon=True).start()
        return True

    def _merge(self, serial: str, report: dict, deep: bool) -> None:
        """Junta la rápida con lo que sabía la completa (decisión 186).
        `report["deep"]`: los recursos cuyo estado viene de una completa (o se
        sabe igual por un despliegue), con su hora."""
        state, now = report["state"], report["checked_at"]
        if deep:
            self.deep[serial] = {g: (v, now) for g, v in state.items()}
            report["deep"] = {g: now for g in state}
            return
        known = self.deep.setdefault(serial, {})
        report["deep"] = {}
        for g, v in state.items():
            if v == "ok" and g in known and known[g][0] in ("ok", "distinta", "corregir"):
                state[g], report["deep"][g] = known[g]
            elif v == "falta":
                known.pop(g, None)              # ya no lo tiene: lo de la completa no vale

    def forget(self, guids, serials=None) -> None:
        """Lo que ha cambiado (en el robot o en el catálogo): la completa ya no
        sabe cómo está; hasta la próxima, solo la rápida (si lo tiene)."""
        for s in list(self.deep) if serials is None else serials:
            for g in guids:
                self.deep.get(s, {}).pop(g, None)

    def same(self, serial: str, guids) -> None:
        """En `serial` estos recursos son ya iguales que el catálogo (se acaban
        de desplegar, o de traer de ahí al catálogo)."""
        known, at = self.deep.setdefault(serial, {}), now_iso()
        for g in guids:
            known[g] = ("ok", at)
            if g in self.brought and serial in self.brought[g]["pending"]:
                self.brought[g]["pending"].remove(serial)

    def catalog_changed(self, serial: str, guids) -> None:
        """Se ha escrito en el catálogo la versión de `serial` (traer, capturar):
        ahí es igual; en los demás robots, la completa ya no sabe."""
        self.forget(guids, [s for s in self.deep if s != serial])
        others = sorted(s for s in self.syncs if s != serial)
        for g in guids:
            self.brought[g] = {"from": serial, "at": now_iso(), "pending": list(others)}
        self.same(serial, guids)

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
