"""Aplicar en caliente una configuración nueva (decisión 91).

Qué se aplica en caliente: lo "ligero", lo que el FM consulta en cada tick o
en cada order — umbrales de auto-carga, `assigner.*`, `battery_min` y qué
actions tiene cada robot (y su definición). Lo "estructural" (robots que
entran o salen, host, driver, manufacturer, bloque `drivers:`, MQTT) pide
reiniciar el FM: un robot nuevo necesita su cliente MQTT con Last Will, su
hilo de poll y su `connection` retained, y eso solo se monta al arrancar.

Hilos: la web pide (`request`, desde su hilo) y el hilo principal aplica
(`apply_pending`, al inicio del tick, como `Dispatcher.drain`), así que la
config cambia siempre entre dos ticks y nunca a mitad de despachar una order
(decisión 14). Una order en curso no se ve afectada: su `Job` ya está
traducido. Lo único con red (volver a cargar los índices de missions del
robot) lo hace el propio robot en su hilo de poll (`Robot.reindex`).
"""
from __future__ import annotations

import logging
import queue
from typing import Callable

from fm.adapters import make_driver
from fm.config import FleetConfig

log = logging.getLogger("fm.reload")

# Claves de `robots.<serial>` que se aplican en caliente; cualquier otra que
# cambie (host, pose…) es del driver y pide reiniciar.
HOT_ROBOT_KEYS = {"actions", "battery_min"}


def restart_reasons(old: FleetConfig, new: FleetConfig, robots: dict) -> list[str]:
    """Por qué `new` no se puede aplicar en caliente sobre `old` (vacío = sí se
    puede). `robots` son los que corren: un driver sin `reconfigure()` no
    admite cambiar sus actions en caliente."""
    reasons = []
    added, removed = sorted(set(new.robots) - set(old.robots)), sorted(set(old.robots) - set(new.robots))
    if added:
        reasons.append(f"robots nuevos: {', '.join(added)}")
    if removed:
        reasons.append(f"robots quitados: {', '.join(removed)}")
    for s in sorted(set(old.robots) & set(new.robots)):
        o, n = old.robots[s], new.robots[s]
        keys = sorted(k for k in set(o.raw) | set(n.raw)
                      if k not in HOT_ROBOT_KEYS and o.raw.get(k) != n.raw.get(k))
        if keys:
            reasons.append(f"robots.{s}: cambia {', '.join(keys)}")
        r = robots.get(s)
        if (r is not None and o.raw.get("actions") != n.raw.get("actions")
                and not hasattr(r.driver, "reconfigure")):
            reasons.append(f"robots.{s}: el driver '{n.driver}' no admite cambiar sus actions en caliente")
    if old.drivers != new.drivers:
        reasons.append("cambia el bloque drivers:")
    if old.mqtt != new.mqtt:
        reasons.append("cambia la configuración MQTT")
    return reasons


class Reloader:
    """Cola de configs pendientes de aplicar + la aplicación en el hilo principal.

    `targets` son los objetos que guardan la config en `.cfg` (Dispatcher,
    WebServer); `publish_factsheet(robot)` republica el factsheet de un robot
    cuyas actions cambian; `notify(dict)` avisa a la web del resultado."""

    def __init__(self, cfg: FleetConfig, robots: dict, targets: list,
                 publish_factsheet: Callable = lambda r: None,
                 notify: Callable[[dict], None] = lambda p: None):
        self.cfg = cfg
        self.robots = robots
        self.targets = targets
        self.publish_factsheet = publish_factsheet
        self.notify = notify
        self.inbox: queue.Queue[FleetConfig] = queue.Queue()

    def request(self, new: FleetConfig) -> list[str]:
        """Hilo web. Encola `new` si se puede aplicar en caliente; si no,
        devuelve los motivos y no encola nada."""
        reasons = restart_reasons(self.cfg, new, self.robots)
        if not reasons:
            self.inbox.put(new)
        return reasons

    def apply_pending(self) -> None:
        """Hilo principal, al inicio del tick. Si hay varias pendientes, vale
        la última (cada una es el fichero entero)."""
        new = None
        while not self.inbox.empty():
            new = self.inbox.get_nowait()
        if new is None:
            return
        reasons = restart_reasons(self.cfg, new, self.robots)    # pudo cambiar desde `request`
        if reasons:
            log.warning("[config] no se aplica en caliente: %s", "; ".join(reasons))
            self.notify({"applied": False, "restart": reasons})
            return
        try:
            changed = self._apply(new)
        except Exception as e:           # p.ej. el driver rechaza su bloque: la config vieja sigue
            log.exception("[config] no se pudo aplicar")
            self.notify({"applied": False, "error": str(e)})
            return
        log.info("[config] aplicada en caliente%s", f" (actions de {', '.join(changed)})" if changed else "")
        self.notify({"applied": True})

    def _apply(self, new: FleetConfig) -> list[str]:
        # Primero todo lo que puede fallar (construir drivers), luego los cambios:
        # si un driver no acepta su bloque, no queda nada a medias.
        fresh = {s: make_driver(new.robots[s], new) for s, r in self.robots.items()
                 if new.robots[s].raw.get("actions") != r.cfg.raw.get("actions")}
        for s, r in self.robots.items():
            r.cfg = new.robots[s]
            r.charge.cfg = new.auto_charge
            r.site = new.site
            if s in fresh:
                r.driver.reconfigure(fresh[s])
                r.reindex = True             # índices de las missions nuevas, en su hilo de poll
        for t in self.targets:
            t.cfg = new
        self.cfg = new
        for s in fresh:                      # cambian sus agvActions
            self.publish_factsheet(self.robots[s])
        return sorted(fresh)
