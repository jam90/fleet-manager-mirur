"""Servidor web del FM: sirve la página y hace de espejo del bus MQTT.

Principio (docs/plan-ui.md): la UI es un cliente VDA más.

- Salida: cada publicación del FM (`state`, `connection`, `order_response`)
  se replica a los navegadores por WebSocket tal cual, `{topic, payload}`.
- Entrada: lo que la UI envía se publica en el broker con header VDA
  (`fleet/order`, `<serial>/instantActions`) y el FM lo recibe por su
  suscripción normal, por el mismo camino que cualquier otro emisor.

Concurrencia: uvicorn corre en un hilo propio con su event loop. Con el hilo
principal comparte solo `MqttBus` (paho es thread-safe para `publish`;
`HeaderCounter` lleva Lock) y datos de configuración de solo lectura. El
estado en vivo de los robots NO se lee desde aquí: llega por el espejo.

API REST (decisión 54): las lecturas (`/api/robots…`, `/api/orders…`,
`/api/health`) salen de lo que el espejo ha visto pasar, no de `Robot`. Así
un cliente HTTP ve lo mismo que un suscriptor MQTT, y el hilo web no toca el
estado del hilo principal. La única estructura compartida que el hilo web
recorre (`orders`) va con un Lock.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from fm.adapters.base import ActionInfo
from fm.config import FleetConfig
from fm.mqtt_bus import FLEET, MqttBus
from fm.robot import Robot
from fm.vda5050.header import parse_topic, topic
from fm.vda5050.state import E_ROBOT_UNREACHABLE

log = logging.getLogger("fm.web")
STATIC = Path(__file__).resolve().parent / "static"

INSTANT_ALLOWED = ("startPause", "stopPause", "cancelOrder", "stateRequest")

# Orders recordadas para `/api/orders`. Vive en RAM (como todo el FM): basta
# con que un cliente pueda consultar las de la última jornada.
ORDERS_MAX = 200
WAIT_MAX_S = 30.0

# Ciclo de vida de una order vista por la API. PENDING = enviada al broker sin
# veredicto aún; ASSIGNED (fleet/order) / ACCEPTED (order dirigida) = el FM la
# aceptó; luego RUNNING → FINISHED | FAILED según `actionStates`.
PENDING, ASSIGNED, ACCEPTED, REJECTED = "PENDING", "ASSIGNED", "ACCEPTED", "REJECTED"
RUNNING, FINISHED, FAILED = "RUNNING", "FINISHED", "FAILED"


class OrderIn(BaseModel):
    actionType: str = Field(description="actionType de fleet.yaml (p.ej. 'coger')")
    params: dict[str, Any] = Field(default={}, description="actionParameters como {key: value}")
    orderId: str | None = Field(default=None, min_length=1,
                                description="opcional: id propio del sistema que envía; si no, 'ui-<hex>'")


class InstantIn(BaseModel):
    actionType: str


def _refs_order(error: dict, order_id: str) -> bool:
    return any(r.get("referenceKey") == "orderId" and r.get("referenceValue") == order_id
               for r in error.get("errorReferences") or [])


def _phase(action_states: list[dict]) -> str | None:
    """Fase de la order a partir de sus `actionStates` (None = aún no arranca)."""
    st = [a.get("actionStatus") for a in action_states]
    if not st:
        return None
    if FAILED in st:
        return FAILED
    if all(x == FINISHED for x in st):
        return FINISHED
    if any(x in ("INITIALIZING", RUNNING, "PAUSED") for x in st):
        return RUNNING
    return None


class WebServer:
    def __init__(self, cfg: FleetConfig, robots: dict[str, Robot], bus: MqttBus):
        self.cfg, self.robots, self.bus = cfg, robots, bus
        self.app = FastAPI(title="Fleet Manager", docs_url="/api/docs")
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[WebSocket] = set()
        # Último mensaje por topic (state/connection): lo que recibe un navegador al conectar.
        self.last: dict[str, dict] = {}
        # orderId → seguimiento (ver `_track_*`). Lo escribe el hilo principal
        # (espejo) y lo leen las peticiones HTTP: Lock.
        self.orders: OrderedDict[str, dict] = OrderedDict()
        self._orders_lock = threading.Lock()
        # orderId → [(loop, future)] de peticiones con `?wait=` esperando veredicto.
        self._waiters: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Future]]] = {}
        self._routes()
        # El gancho se instala ya (antes de `bus.start()`): así los `connection`
        # ONLINE del arranque quedan en `last` aunque uvicorn arranque después.
        self.bus.on_publish = self.mirror

    # ---------------------------------------------------------------- espejo
    def mirror(self, topic: str, payload: dict) -> None:
        """Gancho `bus.on_publish`: se llama desde el hilo principal."""
        if topic.endswith(("/state", "/connection", "/factsheet")):
            self.last[topic] = payload
        pt = parse_topic(topic)
        if pt is not None and pt.subtopic == "order_response":
            self._track_response(payload)
        elif pt is not None and pt.subtopic == "state":
            self._track_state(pt.serial, payload)
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

    # ------------------------------------------------------- seguimiento orders
    def _order(self, order_id: str, **init) -> dict:
        """Entrada de `orders` (la crea si no existe). Llamar con el Lock cogido."""
        rec = self.orders.get(order_id)
        if rec is None:
            rec = {"orderId": order_id, "orderUpdateId": 0, "target": None, "actionType": None,
                   "serial": None, "status": PENDING, "description": None, "errorType": None,
                   "actionStates": [], "errors": [], "created": time.time(), "updated": None, **init}
            self.orders[order_id] = rec
            while len(self.orders) > ORDERS_MAX:
                self.orders.popitem(last=False)
        return rec

    def _track_response(self, p: dict) -> None:
        """`fleet/order_response`: veredicto del asignador."""
        order_id = p.get("orderId")
        if not order_id:
            return               # rechazo de un mensaje sin orderId: nada que seguir
        with self._orders_lock:
            rec = self._order(order_id, target="fleet")
            rec["orderUpdateId"] = p.get("orderUpdateId", 0)
            rec["updated"] = p.get("timestamp")
            if p.get("status") == "ASSIGNED":
                rec.update(status=ASSIGNED, serial=p.get("assignedSerial"), description=p.get("description"))
            else:
                rec.update(status=REJECTED, errorType=p.get("errorType"),
                           description=p.get("errorDescription"))
        self._wake(order_id)

    def _track_state(self, serial: str, p: dict) -> None:
        """`state` de un robot: progreso de su order activa y rechazos de
        orders dirigidas (llegan como `errors[]` con referencia al orderId)."""
        woken = []
        with self._orders_lock:
            current = p.get("orderId") or ""
            if current:
                rec = self._order(current, serial=serial)
                if rec["status"] in (PENDING, REJECTED):
                    rec["status"] = ACCEPTED if rec["target"] != "fleet" else ASSIGNED
                    woken.append(current)
                rec["serial"] = serial
                rec["orderUpdateId"] = p.get("orderUpdateId", rec["orderUpdateId"])
                rec["actionStates"] = p.get("actionStates", [])
                rec["errors"] = [e for e in p.get("errors", []) if _refs_order(e, current)]
                rec["status"] = _phase(rec["actionStates"]) or rec["status"]
                rec["updated"] = p.get("timestamp")
            # Order dirigida rechazada: el state sigue con la order anterior y
            # el motivo va en errors[] apuntando al orderId rechazado.
            for e in p.get("errors", []):
                for r in e.get("errorReferences") or []:
                    oid = r.get("referenceValue")
                    if r.get("referenceKey") != "orderId" or oid == current or oid not in self.orders:
                        continue
                    rec = self.orders[oid]
                    if rec["status"] == PENDING:
                        rec.update(status=REJECTED, serial=serial, errorType=e.get("errorType"),
                                   description=e.get("errorDescription"), errors=[e],
                                   updated=p.get("timestamp"))
                        woken.append(oid)
        for oid in woken:
            self._wake(oid)

    def order_info(self, order_id: str) -> dict | None:
        with self._orders_lock:
            rec = self.orders.get(order_id)
            return dict(rec) if rec is not None else None

    def _wake(self, order_id: str) -> None:
        """Despierta a las peticiones `?wait=` de esa order (desde cualquier hilo)."""
        for loop, fut in self._waiters.pop(order_id, []):
            loop.call_soon_threadsafe(lambda f=fut: f.done() or f.set_result(None))

    async def _wait_verdict(self, order_id: str, wait: float) -> tuple[int, dict]:
        """Espera hasta `wait` s a que la order deje de estar PENDING.
        Devuelve (código HTTP, seguimiento): 200 con veredicto, 202 si no llegó."""
        if wait > 0:
            fut = asyncio.get_running_loop().create_future()
            self._waiters.setdefault(order_id, []).append((asyncio.get_running_loop(), fut))
            # Puede que el veredicto llegara entre publicar y registrarse.
            rec = self.order_info(order_id)
            if rec is None or rec["status"] == PENDING:
                try:
                    await asyncio.wait_for(fut, wait)
                except asyncio.TimeoutError:
                    pass
        rec = self.order_info(order_id) or {"orderId": order_id, "status": PENDING}
        return (202 if rec["status"] == PENDING and wait > 0 else 200), rec

    def _new_order(self, order_id: str, target: str, action_type: str, serial: str | None) -> None:
        with self._orders_lock:
            self._order(order_id, target=target, actionType=action_type, serial=serial)

    # ------------------------------------------------------------- snapshot
    def fleet_info(self) -> dict:
        robots = []
        for s, r in self.robots.items():
            describe = getattr(r.driver, "describe_actions", None)
            if describe is not None:
                actions = [asdict(a) for a in describe()]
            else:
                actions = [asdict(ActionInfo(t)) for t in sorted(r.cfg.action_types)]
            robots.append({"serial": s, "manufacturer": r.manufacturer, "driver": r.cfg.driver,
                           "battery_min": r.cfg.battery_min, "actions": actions})
        return {"robots": robots,
                "fleet_manufacturer": self.cfg.mqtt.fleet_manufacturer,
                "auto_charge": asdict(self.cfg.auto_charge),
                "instant_actions": list(INSTANT_ALLOWED)}

    def robot_summary(self, serial: str) -> dict:
        """Resumen plano de un robot a partir de su último `state`/`connection`."""
        man = self.robots[serial].manufacturer
        st = self.last.get(topic(man, serial, "state"))
        conn = self.last.get(topic(man, serial, "connection"))
        out = {"serial": serial, "manufacturer": man,
               "connection": conn.get("connectionState") if conn else None}
        if st is None:
            return {**out, "state": None}
        ps = st.get("powerSupply") or {}
        errors = st.get("errors", [])
        return {**out,
                "reachable": not any(e.get("errorType") == E_ROBOT_UNREACHABLE for e in errors),
                "battery": ps.get("stateOfCharge"), "charging": ps.get("charging"),
                "driving": st.get("driving"), "paused": st.get("paused"),
                "operatingMode": st.get("operatingMode"),
                "position": st.get("mobileRobotPosition"),
                "orderId": st.get("orderId"), "orderUpdateId": st.get("orderUpdateId"),
                "actionStates": st.get("actionStates", []),
                "instantActionStates": st.get("instantActionStates", []),
                "errors": errors, "information": st.get("information", []),
                "timestamp": st.get("timestamp")}

    # ------------------------------------------------------------- entradas
    @staticmethod
    def _order_body(order_id: str, action_type: str, params: dict) -> dict:
        """Cuerpo VDA de una order de un nodo y una action (límite del FM, §5.4)."""
        return {"orderId": order_id, "orderUpdateId": 0, "edges": [],
                "nodes": [{"nodeId": "N0", "sequenceId": 0, "released": True,
                           "actions": [{"actionType": action_type, "actionId": f"{order_id}-a1",
                                        "blockingType": "HARD",
                                        "actionParameters": [{"key": k, "value": v} for k, v in params.items()]}]}]}

    def send_fleet_order(self, action_type: str, params: dict, order_id: str | None = None) -> str:
        order_id = order_id or f"ui-{uuid.uuid4().hex[:8]}"
        self._new_order(order_id, "fleet", action_type, None)   # antes de publicar: el veredicto puede ser inmediato
        self.bus.publish(FLEET, "order", self._order_body(order_id, action_type, params))
        log.info("[web] fleet/order %s %s %s", order_id, action_type, params or "")
        return order_id

    def send_robot_order(self, serial: str, action_type: str, params: dict,
                         order_id: str | None = None) -> str:
        order_id = order_id or f"ui-{uuid.uuid4().hex[:8]}"
        self._new_order(order_id, "robot", action_type, serial)
        self.bus.publish(serial, "order", self._order_body(order_id, action_type, params))
        log.info("[web] %s/order %s %s %s", serial, order_id, action_type, params or "")
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

        def robot_or_404(serial: str) -> Robot:
            if serial not in self.robots:
                raise HTTPException(404, f"robot '{serial}' desconocido")
            return self.robots[serial]

        def last_or_503(serial: str, sub: str) -> dict:
            msg = self.last.get(topic(robot_or_404(serial).manufacturer, serial, sub))
            if msg is None:
                raise HTTPException(503, f"aún no se ha publicado {sub} de '{serial}'")
            return msg

        def reply(code: int, rec: dict) -> JSONResponse:
            return JSONResponse(rec, status_code=code)

        wait_q = Query(0.0, ge=0.0, le=WAIT_MAX_S,
                       description="segundos a esperar el veredicto (0 = no esperar)")

        @app.get("/api/health")
        async def health():
            robots = {s: self.robot_summary(s) for s in self.robots}
            return {"ok": all(r["connection"] == "ONLINE" and r.get("reachable") for r in robots.values()),
                    "robots": {s: {"connection": r["connection"], "reachable": r.get("reachable"),
                                   "lastState": r.get("timestamp")} for s, r in robots.items()}}

        @app.get("/api/fleet")
        async def fleet():
            return self.fleet_info()

        @app.post("/api/fleet/order", summary="Orden a la flota: el FM elige el robot")
        async def fleet_order(body: OrderIn, wait: float = wait_q):
            known = {a["action_type"] for r in self.fleet_info()["robots"] for a in r["actions"]}
            if body.actionType not in known:
                raise HTTPException(400, f"actionType '{body.actionType}' desconocido; hay {sorted(known)}")
            order_id = self.send_fleet_order(body.actionType, body.params, body.orderId)
            return reply(*await self._wait_verdict(order_id, wait))

        @app.get("/api/robots")
        async def robots():
            return [self.robot_summary(s) for s in self.robots]

        @app.get("/api/robots/{serial}")
        async def robot(serial: str):
            robot_or_404(serial)
            return self.robot_summary(serial)

        @app.get("/api/robots/{serial}/state", summary="Último state VDA 5050, tal cual")
        async def robot_state(serial: str):
            return last_or_503(serial, "state")

        @app.get("/api/robots/{serial}/connection")
        async def robot_connection(serial: str):
            return last_or_503(serial, "connection")

        @app.get("/api/robots/{serial}/factsheet")
        async def robot_factsheet(serial: str):
            return last_or_503(serial, "factsheet")

        @app.get("/api/robots/{serial}/actions", summary="actionTypes que acepta el robot y sus parámetros")
        async def robot_actions(serial: str):
            robot_or_404(serial)
            return next(r["actions"] for r in self.fleet_info()["robots"] if r["serial"] == serial)

        @app.post("/api/robots/{serial}/order", summary="Orden dirigida a un robot (sin asignador)")
        async def robot_order(serial: str, body: OrderIn, wait: float = wait_q):
            robot_or_404(serial)
            known = {a["action_type"] for a in await robot_actions(serial)}
            if body.actionType not in known:
                raise HTTPException(400, f"'{serial}' no admite '{body.actionType}'; admite {sorted(known)}")
            order_id = self.send_robot_order(serial, body.actionType, body.params, body.orderId)
            return reply(*await self._wait_verdict(order_id, wait))

        @app.get("/api/orders", summary="Orders recientes (la más nueva primero)")
        async def orders(limit: int = Query(50, ge=1, le=ORDERS_MAX)):
            with self._orders_lock:
                recs = [dict(r) for r in reversed(self.orders.values())]
            return recs[:limit]

        @app.get("/api/orders/{order_id}")
        async def order(order_id: str):
            rec = self.order_info(order_id)
            if rec is None:
                raise HTTPException(404, f"order '{order_id}' no vista por este FM")
            return rec

        @app.post("/api/robots/{serial}/instant")
        async def instant(serial: str, body: InstantIn):
            robot_or_404(serial)
            if body.actionType not in INSTANT_ALLOWED:
                raise HTTPException(400, f"instantAction '{body.actionType}' no admitida; hay {INSTANT_ALLOWED}")
            return {"actionId": self.send_instant(serial, body.actionType)}

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
        log.info("[web] interfaz en http://%s:%d/", host, port)

    def stop(self) -> None:
        self.bus.on_publish = None
        if getattr(self, "_server", None) is not None:
            self._server.should_exit = True
