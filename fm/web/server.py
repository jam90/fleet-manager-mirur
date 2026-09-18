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
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fm.adapters.base import ActionInfo
from fm.config import FleetConfig
from fm.mqtt_bus import FLEET, MqttBus
from fm.robot import Robot

log = logging.getLogger("fm.web")
STATIC = Path(__file__).resolve().parent / "static"

INSTANT_ALLOWED = ("startPause", "stopPause", "cancelOrder", "stateRequest")


class FleetOrderIn(BaseModel):
    actionType: str
    params: dict[str, Any] = {}


class InstantIn(BaseModel):
    actionType: str


class WebServer:
    def __init__(self, cfg: FleetConfig, robots: dict[str, Robot], bus: MqttBus):
        self.cfg, self.robots, self.bus = cfg, robots, bus
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
        """Gancho `bus.on_publish`: se llama desde el hilo principal."""
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
