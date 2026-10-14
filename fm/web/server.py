"""Servidor web del FM: sirve la página y hace de espejo del bus MQTT.

Principio (docs/plan-ui.md): la UI es un cliente VDA más.

- Salida: cada publicación del FM (`state`, `connection`, `order_response`)
  se replica a los navegadores por WebSocket tal cual, `{topic, payload}`.
- Entrada: lo que la UI envía se publica en el broker con header VDA
  (`fleet/order`, `<serial>/instantActions`) y el FM lo recibe por su
  suscripción normal, por el mismo camino que cualquier otro emisor.

Sincronización (docs/plan-admin.md §3): si el driver de un robot ofrece
`make_sync(catalog_dir)` (hoy, solo MiR), la pestaña Sincronización puede
previsualizar y desplegar en él una selección de `catalog/`. El despliegue
corre en un hilo propio por robot (nunca en el bucle de 1 Hz) y su avance se
emite por el mismo WebSocket con el topic `fm/sync/<serial>`. Además,
`SyncMonitor` comprueba cada 5 min qué tiene cada robot del catálogo
(`sync_monitor.py`) y emite el informe por el mismo topic.

Configurador (docs/plan-admin.md §2): `GET /api/config` da lo que el FM leyó
de `fleet.yaml` (catálogo de actions, qué robot tiene cada una, umbrales). Si
la sincronización está disponible, añade qué recursos del catálogo necesita
cada action; la pestaña lo cruza con los informes de `SyncMonitor` para
pintar en cada casilla si el robot tiene la mission. `POST /api/config` guarda
ediciones (casillas, `battery_min`, umbrales) en `fleet.yaml` sin tocar el
resto del fichero (`fm/config_edit.py`) y pide al `Reloader` que la config
nueva se aplique en caliente entre ticks (`fm/reload.py`); el resultado llega
por el WebSocket con el topic `fm/config`.

Crear (docs/plan-admin.md §3, `fm/adapters/mir/create.py`): si el objeto de
sincronización lo ofrece, la pestaña Crear da de alta en un robot una
mission vacía o una position (en la posición actual del robot) con GUID
estándar, la apunta en `catalog/` como borrador y luego la captura cuando el
usuario la ha terminado en la web del MiR.

Concurrencia: uvicorn corre en un hilo propio con su event loop. Con el hilo
principal comparte solo `MqttBus` (paho es thread-safe para `publish`;
`HeaderCounter` lleva Lock) y datos de configuración de solo lectura. El
estado en vivo de los robots NO se lee desde aquí: llega por el espejo.
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
import logging
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fm.adapters.base import describe_actions
from fm.config import ConfigError, FleetConfig, load_config
from fm.config_edit import EditError, save, validate
from fm.mqtt_bus import FLEET, MqttBus
from fm.robot import Robot
from fm.web.sync_monitor import SyncMonitor

log = logging.getLogger("fm.web")
STATIC = Path(__file__).resolve().parent / "static"
CATALOG = Path(__file__).resolve().parents[2] / "catalog"

INSTANT_ALLOWED = ("startPause", "stopPause", "cancelOrder", "stateRequest")


class FleetOrderIn(BaseModel):
    actionType: str
    params: dict[str, Any] = {}


class InstantIn(BaseModel):
    actionType: str


class SyncIn(BaseModel):
    guids: list[str]


class CheckIn(BaseModel):
    deep: bool = False


class CreateIn(BaseModel):
    name: str
    parent: str          # GUID del catálogo: grupo (mission) o mapa (position)


class CaptureIn(BaseModel):
    guid: str


class ConfigEditIn(BaseModel):
    """Ediciones del configurador; ver `fm.config_edit.plan`."""
    actions: dict[str, dict[str, Any] | None] = {}
    robots: dict[str, dict[str, Any]] = {}
    auto_charge: dict[str, Any] = {}
    assigner: dict[str, Any] = {}


class WebServer:
    def __init__(self, cfg: FleetConfig, robots: dict[str, Robot], bus: MqttBus,
                 catalog_dir: Path = CATALOG, config_path: str | Path | None = None, env_path=None):
        self.cfg, self.robots, self.bus = cfg, robots, bus
        # Edición de la config: sin `config_path` o sin `reloader` (lo pone
        # run_fm, que es quien tiene el hilo principal) la pestaña es de solo lectura.
        self.config_path = Path(config_path) if config_path else None
        self.env_path = env_path
        self.reloader = None
        self._save_lock = threading.Lock()
        self._create_lock = threading.Lock()   # dos altas a la vez sacarían el mismo número
        # Sincronización: solo los robots cuyo driver la ofrece (capacidad opcional).
        self.syncs = {s: r.driver.make_sync(catalog_dir) for s, r in robots.items()
                      if hasattr(r.driver, "make_sync")}
        self._deploying: set[str] = set()
        self._deploy_lock = threading.Lock()
        self.monitor = SyncMonitor(self.syncs, robots,
                                   publish=lambda s, p: self.mirror(f"fm/sync/{s}", p))
        self.app = FastAPI(title="Fleet Manager", docs_url="/api/docs")
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[WebSocket] = set()
        # Último mensaje por topic (state/connection): lo que recibe un navegador al conectar.
        self.last: dict[str, dict] = {}
        self._routes()
        # El gancho se instala ya (antes de `bus.start()`): así los `connection`
        # ONLINE del arranque quedan en `last` aunque uvicorn arranque después.
        self.bus.on_publish = self.mirror

    # ---------------------------------------------------------------- espejo
    def mirror(self, topic: str, payload: dict) -> None:
        """Gancho `bus.on_publish`. Se llama desde el hilo que publica: el
        principal casi siempre, pero también paho (`connection` ONLINE al
        reconectar) y el propio hilo web (las orders de la UI). Por eso solo
        hace operaciones atómicas y delega el envío en el event loop."""
        if topic.endswith("/state") or topic.endswith("/connection"):
            self.last[topic] = payload
        if self.loop is not None and self.clients:
            self.loop.call_soon_threadsafe(self._schedule_broadcast, topic, payload)

    def _schedule_broadcast(self, topic: str, payload: dict) -> None:
        asyncio.ensure_future(self._broadcast({"topic": topic, "payload": payload}))

    async def _broadcast(self, msg: dict) -> None:
        data = json.dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.discard(ws)

    # ------------------------------------------------------------- snapshot
    def fleet_info(self) -> dict:
        robots = []
        for s, r in self.robots.items():
            actions = [asdict(a) for a in describe_actions(r.driver, r.cfg.action_types)]
            robots.append({"serial": s, "manufacturer": r.manufacturer, "driver": r.cfg.driver,
                           "battery_min": r.cfg.battery_min, "actions": actions})
        return {"robots": robots,
                "fleet_manufacturer": self.cfg.mqtt.fleet_manufacturer,
                "auto_charge": asdict(self.cfg.auto_charge),
                "instant_actions": list(INSTANT_ALLOWED)}

    def config_info(self) -> dict:
        """Configuración en uso, para la pestaña Configurador. Si `fleet.yaml`
        se ha editado a mano y no coincide, `file` lo dice (y si pide reiniciar)."""
        robots = []
        for s, r in self.robots.items():
            rc = r.cfg
            settings = {k: v for k, v in rc.raw.items()
                        if k not in ("actions", "driver", "manufacturer", "battery_min")}
            robots.append({"serial": s, "driver": rc.driver, "manufacturer": r.manufacturer,
                           "battery_min": rc.battery_min, "actions": sorted(rc.action_types),
                           "settings": settings, "sync": s in self.syncs})
        # Qué necesita cada action del catálogo: no depende del robot (el
        # catálogo es común), así que basta con el primer robot que sincroniza.
        first = next(iter(self.syncs.values()), None)
        needs = {}
        if first is not None and hasattr(first, "action_needs"):
            try:
                needs = first.action_needs(self.cfg.actions)
            except Exception as e:          # catálogo roto: la pestaña sigue, sin casillas de estado
                log.warning("[web] no se pudo cruzar el catálogo de actions con catalog/: %s", e)
        # Para el formulario de actions: missions del catálogo y sus inputs.
        choices = None
        if first is not None and hasattr(first, "form_choices"):
            try:
                choices = first.form_choices()
            except Exception as e:
                log.warning("[web] no se pudieron leer las missions de catalog/: %s", e)
        return {"actions": [{"name": n, "definition": d, "needs": needs.get(n)}
                            for n, d in self.cfg.actions.items()],
                "choices": choices,
                "robots": robots,
                "auto_charge": asdict(self.cfg.auto_charge),
                "assigner": {"prefer_not_charging": self.cfg.prefer_not_charging},
                "drivers": self.cfg.drivers,
                "fleet_manufacturer": self.cfg.mqtt.fleet_manufacturer,
                "editable": self.config_path is not None and self.reloader is not None,
                "file": self.file_status()}

    def file_status(self) -> dict | None:
        """¿El `fleet.yaml` del disco es la config en uso? Si no (editado a
        mano), qué impide aplicarlo en caliente."""
        if self.config_path is None:
            return None
        try:
            disk = load_config(self.config_path, self.env_path)
        except ConfigError as e:
            return {"same": False, "error": str(e)}
        if disk == self.cfg:
            return {"same": True}
        from fm.reload import restart_reasons
        return {"same": False, "restart": restart_reasons(self.cfg, disk, self.robots)}

    # ------------------------------------------------------------- crear
    def create_info(self) -> dict:
        """Robots donde se puede crear y lo que ofrece el catálogo (grupos,
        mapas, borradores pendientes de capturar, con su enlace al MiR)."""
        robots = [{**self.sync_status(s), "can_create": s in self.syncs and hasattr(self.syncs[s], "create_mission")}
                  for s in self.robots]
        first = self._first_creator()
        choices = first.create_choices() if first else {"groups": [], "maps": [], "created": []}
        for d in choices["created"]:
            y = self.syncs.get(d["robot"])
            d["url"] = y.web_url(d["guid"]) if y is not None else None
        return {"robots": robots, **choices}

    def _first_creator(self, capability: str = "create_choices"):
        """El catálogo es común: basta con el primer robot que ofrezca `capability`."""
        return next((y for y in self.syncs.values() if hasattr(y, capability)), None)

    def delete_plan(self, guid: str, retire: bool = False) -> dict:
        """Qué pasaría al quitar `guid` de la flota (todos los robots y el
        catálogo): en qué robots está y qué lo impide. `retire=False` es el
        «Borrar» de la pestaña Crear (solo lo creado allí; libera el número);
        `retire=True`, «Retirar de la flota» en Sincronización (cualquier
        mission o position del catálogo; el número queda reservado). Se mira
        en TODOS los robots: uno sin conexión podría guardar una copia."""
        first = self._first_creator("catalog_item" if retire else "describe")
        info = (first.catalog_item(guid) if retire else first.describe(guid)) if first else None
        if info is None:
            raise HTTPException(404, f"{guid} no está en el catálogo" if retire
                                else f"{guid} no es algo creado desde la pestaña Crear")
        wanted = [w for y in self.syncs.values() for w in getattr(y, "wanted", [])]
        check = first.retire_blockers if retire else first.delete_blockers
        blockers = check(guid, self.cfg.actions, wanted)
        where = []
        for s, y in self.syncs.items():
            if self.robots[s].last_telemetry is None:
                blockers.append(f"{s} no responde: podría tener una copia")
                continue
            try:
                if y.exists(guid):
                    where.append(s)
                    if self.robots[s].busy:
                        blockers.append(f"{s} está ocupado: espera a que quede libre")
                    if s in self._deploying:
                        blockers.append(f"{s} está desplegando")
            except Exception as e:
                blockers.append(f"no se pudo mirar {s}: {e}")
        return {**info, "robots": where, "blockers": blockers}

    def delete(self, guid: str, retire: bool = False) -> dict:
        """Quita `guid` de los robots que lo tienen y del catálogo. Si falla a
        medias, el catálogo no se toca: se puede volver a intentar."""
        with self._create_lock:
            plan = self.delete_plan(guid, retire)
            if plan["blockers"]:
                raise HTTPException(409, "no se quita: " + "; ".join(plan["blockers"]))
            for s in plan["robots"]:
                try:
                    self.syncs[s].delete(guid)
                except Exception as e:
                    raise HTTPException(502, f"no se pudo borrar en {s}: {e} (lo demás sigue igual; reinténtalo)")
            gone = self._first_creator("forget").forget(guid, retire, plan["robots"])
        log.info("[web] %s %s %r de %s y del catálogo (%d ficheros)", "retirada" if retire else "borrada",
                 plan["kind"], plan["name"], ", ".join(plan["robots"]) or "ningún robot", len(gone))
        for s in plan["robots"]:
            self.monitor.check_async(s)          # el estado de la flota, al día
        return {**plan, "files": len(gone)}

    def remove_plan(self, serial: str, guid: str) -> dict:
        """«Quitar de este robot»: una mission del catálogo que su fleet.yaml
        no pide. El catálogo no cambia (se puede volver a desplegar)."""
        y = self._sync_for(serial)
        info = y.catalog_item(guid) if hasattr(y, "catalog_item") else None
        if info is None:
            raise HTTPException(404, f"{guid} no está en el catálogo")
        st = self.sync_status(serial)
        blockers = [] if st["online"] else [f"{serial} no responde"]
        if st["busy"]:
            blockers.append(f"{serial} está ocupado: espera a que quede libre")
        if not blockers:
            try:
                blockers = y.remove_blockers(guid)
            except Exception as e:
                blockers = [f"no se pudo mirar {serial}: {e}"]
        return {**info, "robots": [serial], "blockers": blockers}

    def remove(self, serial: str, guid: str) -> dict:
        with self._create_lock:
            plan = self.remove_plan(serial, guid)
            if plan["blockers"]:
                raise HTTPException(409, "no se quita: " + "; ".join(plan["blockers"]))
            try:
                self.syncs[serial].delete(guid)
            except Exception as e:
                raise HTTPException(502, f"no se pudo borrar en {serial}: {e}")
        log.info("[web] quitada de %s la mission %r (sigue en el catálogo)", serial, plan["name"])
        self.monitor.check_async(serial)
        return plan

    def _creator(self, serial: str):
        if serial not in self.robots:
            raise HTTPException(404, f"robot '{serial}' desconocido")
        y = self.syncs.get(serial)
        if y is None or not hasattr(y, "create_mission"):
            raise HTTPException(400, f"el driver de '{serial}' no permite crear recursos")
        if self.robots[serial].last_telemetry is None:
            raise HTTPException(409, f"'{serial}' no responde")
        if serial in self._deploying:
            raise HTTPException(409, f"'{serial}' está desplegando: espera a que termine")
        return y

    def _others_guids(self, serial: str, kind: str, parent: str) -> set[str]:
        """GUIDs estándar de los demás robots que respondan: el número nuevo
        no debe chocar con nada que ya tengan, aunque no esté en el catálogo."""
        out: set[str] = set()
        for s, y in self.syncs.items():
            if s == serial or self.robots[s].last_telemetry is None or not hasattr(y, "std_guids"):
                continue
            try:
                out |= y.std_guids(kind, parent if kind == "position" else None)
            except Exception as e:          # p.ej. ese robot no tiene el mapa: nada que chocar
                log.info("[web] %s: no se pudieron leer sus GUIDs (%s)", s, e)
        return out

    def create(self, serial: str, kind: str, name: str, parent: str) -> dict:
        y = self._creator(serial)
        with self._create_lock:
            try:
                others = self._others_guids(serial, kind, parent)
                out = (y.create_mission if kind == "mission" else y.create_position)(name, parent, others)
            except ValueError as e:         # nombre repetido, falta el grupo/mapa en el robot…
                raise HTTPException(400, str(e))
            except Exception as e:          # red, 4xx/5xx del robot
                raise HTTPException(502, f"{serial}: {e}")
        log.info("[web] creada en %s: %s %r (%s)", serial, kind, out["name"], out["guid"])
        return out

    def capture(self, serial: str, guid: str) -> dict:
        y = self._creator(serial)
        try:
            out = y.capture(guid)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(502, f"{serial}: {e}")
        log.info("[web] capturada de %s: %s %r", serial, out["kind"], out["name"])
        return out

    def _check_editable(self) -> Path:
        if self.config_path is None or self.reloader is None:
            raise HTTPException(501, "este FM no tiene la edición de la config activada")
        return self.config_path

    def save_config(self, edits: dict) -> dict:
        """Guarda las ediciones y pide aplicarlas en caliente (hilo web)."""
        path = self._check_editable()
        with self._save_lock:                # dos navegadores guardando a la vez
            try:
                res = save(path, edits, self.env_path)
            except (EditError, ConfigError) as e:
                raise HTTPException(400, str(e))
            if not res.changed:
                return {"changed": False}
            restart = self.reloader.request(res.cfg)
        log.info("[web] fleet.yaml guardado (copia en %s)%s", res.backup.name,
                 f"; requiere reiniciar: {'; '.join(restart)}" if restart else "")
        return {"changed": True, "backup": res.backup.name, "restart": restart}

    def reload_config(self) -> dict:
        """Aplica el `fleet.yaml` tal como está en disco (editado a mano)."""
        path = self._check_editable()
        try:
            cfg = validate(path, self.env_path)
        except ConfigError as e:
            raise HTTPException(400, str(e))
        if cfg == self.cfg:
            return {"changed": False}
        return {"changed": True, "restart": self.reloader.request(cfg)}

    # ------------------------------------------------------- sincronización
    def sync_status(self, serial: str) -> dict:
        r = self.robots[serial]
        if serial not in self.syncs:
            return {"serial": serial, "supported": False}
        return {"serial": serial, "supported": True, "online": r.last_telemetry is not None,
                "busy": r.busy, "deploying": serial in self._deploying,
                "checking": serial in self.monitor.running}

    def _sync_for(self, serial: str):
        if serial not in self.robots:
            raise HTTPException(404, f"robot '{serial}' desconocido")
        if serial not in self.syncs:
            raise HTTPException(400, f"el driver de '{serial}' no ofrece sincronización")
        if serial in self._deploying:
            raise HTTPException(409, f"ya hay un despliegue en curso en '{serial}'")
        return self.syncs[serial]

    def _run_deploy(self, serial: str, sync, guids: list[str], only_create: bool = False) -> None:
        topic = f"fm/sync/{serial}"
        try:
            progress = lambda op: self.mirror(topic, {"op": op.to_dict()})   # noqa: E731
            ops = (sync.deploy(guids, progress=progress, only_create=True) if only_create
                   else sync.deploy(guids, progress=progress))
            summary = dict(Counter(o.status for o in ops))
            log.info("[web] despliegue en %s terminado: %s", serial, summary)
            self.mirror(topic, {"done": True, "summary": summary})
        except Exception as e:      # red caída al leer el robot, catálogo roto…
            log.exception("[web] despliegue en %s fallido", serial)
            self.mirror(topic, {"done": True, "error": str(e)})
        finally:
            with self._deploy_lock:
                self._deploying.discard(serial)
        self.monitor.check(serial, wait_lock=True)    # el estado de la flota, al día

    # ------------------------------------------------------------- entradas
    def send_fleet_order(self, action_type: str, params: dict) -> str:
        order_id = f"ui-{uuid.uuid4().hex[:8]}"
        self.bus.publish(FLEET, "order", {
            "orderId": order_id, "orderUpdateId": 0, "edges": [],
            "nodes": [{"nodeId": "N0", "sequenceId": 0, "released": True,
                       "actions": [{"actionType": action_type, "actionId": f"{order_id}-a1",
                                    "blockingType": "HARD",
                                    "actionParameters": [{"key": k, "value": v} for k, v in params.items()]}]}]})
        log.info("[web] fleet/order %s %s %s", order_id, action_type, params or "")
        return order_id

    def send_instant(self, serial: str, action_type: str) -> str:
        action_id = f"ui-{uuid.uuid4().hex[:8]}"
        self.bus.publish(serial, "instantActions", {
            "actions": [{"actionType": action_type, "actionId": action_id, "blockingType": "NONE",
                         "actionParameters": []}]})
        log.info("[web] %s/instantActions %s", serial, action_type)
        return action_id

    # ---------------------------------------------------------------- rutas
    def _routes(self) -> None:
        app = self.app

        @app.get("/")
        async def index():
            return FileResponse(STATIC / "index.html")

        @app.get("/api/fleet")
        async def fleet():
            return self.fleet_info()

        @app.get("/api/config")
        def config():                       # `def`: lee catalog/ y fleet.yaml del disco
            return self.config_info()

        @app.post("/api/config")
        def config_save(body: ConfigEditIn):
            return self.save_config(body.model_dump())

        @app.get("/api/create")
        def create_info():                  # `def`: lee catalog/ del disco
            return self.create_info()

        @app.post("/api/create/{serial}/mission")
        def create_mission(serial: str, body: CreateIn):
            return self.create(serial, "mission", body.name, body.parent)

        @app.post("/api/create/{serial}/position")
        def create_position(serial: str, body: CreateIn):
            return self.create(serial, "position", body.name, body.parent)

        @app.post("/api/retire/preview")
        def retire_preview(body: CaptureIn):
            return self.delete_plan(body.guid, retire=True)

        @app.post("/api/retire")
        def retire(body: CaptureIn):
            return self.delete(body.guid, retire=True)

        @app.post("/api/sync/{serial}/remove/preview")
        def remove_preview(serial: str, body: CaptureIn):
            return self.remove_plan(serial, body.guid)

        @app.post("/api/sync/{serial}/remove")
        def remove(serial: str, body: CaptureIn):
            return self.remove(serial, body.guid)

        @app.post("/api/create/delete/preview")
        def delete_preview(body: CaptureIn):
            return self.delete_plan(body.guid)

        @app.post("/api/create/delete")
        def delete(body: CaptureIn):
            return self.delete(body.guid)

        @app.post("/api/create/{serial}/capture")
        def capture(serial: str, body: CaptureIn):
            return self.capture(serial, body.guid)

        @app.post("/api/config/reload")
        def config_reload():
            return self.reload_config()

        @app.post("/api/fleet/order")
        async def fleet_order(body: FleetOrderIn):
            known = {a["action_type"] for r in self.fleet_info()["robots"] for a in r["actions"]}
            if body.actionType not in known:
                raise HTTPException(400, f"actionType '{body.actionType}' desconocido; hay {sorted(known)}")
            return {"orderId": self.send_fleet_order(body.actionType, body.params)}

        @app.post("/api/robots/{serial}/instant")
        async def instant(serial: str, body: InstantIn):
            if serial not in self.robots:
                raise HTTPException(404, f"robot '{serial}' desconocido")
            if body.actionType not in INSTANT_ALLOWED:
                raise HTTPException(400, f"instantAction '{body.actionType}' no admitida; hay {INSTANT_ALLOWED}")
            return {"actionId": self.send_instant(serial, body.actionType)}

        # Las rutas de sincronización son `def` (no `async`): FastAPI las corre en
        # su pool de hilos, porque leen ficheros y hacen REST bloqueante.
        @app.get("/api/sync")
        def sync_info():
            first = next(iter(self.syncs.values()), None)
            return {"catalog": first.catalog().summary() if first else [],
                    "robots": [self.sync_status(s) for s in self.robots],
                    "reports": self.monitor.reports}

        @app.post("/api/sync/{serial}/check")
        def sync_check(serial: str, body: CheckIn):
            self._sync_for(serial)
            if not self.sync_status(serial)["online"]:
                raise HTTPException(409, f"'{serial}' no responde: no se puede comprobar")
            if not self.monitor.check_async(serial, deep=body.deep):
                raise HTTPException(409, f"ya se está comprobando '{serial}'")
            return {"started": True}

        @app.post("/api/sync/{serial}/preview")
        def sync_preview(serial: str, body: SyncIn):
            sync = self._sync_for(serial)
            try:
                _, ops = sync.preview(body.guids)
            except ValueError as e:             # GUID que no está en el catálogo
                raise HTTPException(400, str(e))
            except Exception as e:              # robot inalcanzable, 4xx/5xx del robot…
                raise HTTPException(502, f"no se pudo leer '{serial}': {e}")
            return {"ops": [o.to_dict() for o in ops]}

        @app.post("/api/sync/{serial}/deploy")
        def sync_deploy(serial: str, body: SyncIn):
            sync = self._sync_for(serial)
            st = self.sync_status(serial)
            if not st["online"]:
                raise HTTPException(409, f"'{serial}' no responde: no se despliega")
            # Ocupado (order, carga o mission lanzada desde su web): solo se
            # admite CREAR lo que no tiene, que no toca lo que está ejecutando;
            # corregir un recurso que ya tiene espera a que quede libre (decisión 103).
            only_create = st["busy"]
            if only_create:
                try:
                    _, ops = sync.preview(body.guids)
                except Exception as e:
                    raise HTTPException(502, f"no se pudo leer '{serial}': {e}")
                touch = [o.name for o in ops if o.verb == "corregir"]
                if touch:
                    raise HTTPException(409, f"'{serial}' está ocupado y habría que corregir recursos que ya "
                                             f"tiene ({', '.join(touch)}): se despliega cuando quede libre")
            with self._deploy_lock:
                if serial in self._deploying:
                    raise HTTPException(409, f"ya hay un despliegue en curso en '{serial}'")
                self._deploying.add(serial)
            threading.Thread(target=self._run_deploy, args=(serial, sync, body.guids, only_create),
                             name=f"sync-{serial}", daemon=True).start()
            log.info("[web] despliegue en %s: %d recursos seleccionados", serial, len(body.guids))
            return {"started": True}

        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.websocket("/ws")
        async def ws(websocket: WebSocket):
            await websocket.accept()
            self.clients.add(websocket)
            try:
                for t, p in list(self.last.items()):        # estado actual al conectar
                    await websocket.send_text(json.dumps({"topic": t, "payload": p}))
                while True:
                    await websocket.receive_text()          # la UI no envía por WS; solo mantiene vivo
            except WebSocketDisconnect:
                pass
            finally:
                self.clients.discard(websocket)

    # ------------------------------------------------------------ ciclo vida
    def start(self, host: str = "0.0.0.0", port: int = 8050) -> None:
        import uvicorn

        config = uvicorn.Config(self.app, host=host, port=port, log_level="warning", loop="asyncio")
        server = uvicorn.Server(config)

        def run() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(server.serve())
            except SystemExit:
                pass
            if not server.started:
                log.error("[web] no se pudo abrir el puerto %d (¿ocupado?): interfaz desactivada", port)

        self._server = server
        threading.Thread(target=run, name="web", daemon=True).start()
        self.monitor.start()
        log.info("[web] interfaz en http://%s:%d/", host, port)

    def stop(self) -> None:
        self.monitor.stop()
        self.bus.on_publish = None
        if getattr(self, "_server", None) is not None:
            self._server.should_exit = True
