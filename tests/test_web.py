"""Servidor web: snapshot, envío (publica en el bus) y espejo por WebSocket."""
import json
import threading
import time

from fastapi.testclient import TestClient

from fm.adapters.sim import SimDriver, SimRobotConfig
from fm.config import FleetConfig, RobotConfig
from fm.robot import Robot
from fm.vda5050.header import HeaderCounter, make_header, topic
from fm.web.server import WebServer
from tests.test_fleet import FakeBus


def _server():
    cfg = FleetConfig(robots={"sim-1": RobotConfig("sim-1", driver="sim", battery_min=25,
                                                   action_types=frozenset({"coger", "dejar"}))})
    d = SimDriver(SimRobotConfig("sim-1", {"coger", "dejar"}))
    robots = {"sim-1": Robot(cfg.robots["sim-1"], d)}
    bus = FakeBus({"sim-1": "SIM"})
    bus.on_publish = None
    return WebServer(cfg, robots, bus), bus


def test_fleet_info_y_pagina():
    srv, bus = _server()
    c = TestClient(srv.app)
    assert c.get("/").status_code == 200
    info = c.get("/api/fleet").json()
    assert info["fleet_manufacturer"] == "imperial_fleet"
    r = info["robots"][0]
    assert r["serial"] == "sim-1" and r["manufacturer"] == "SIM" and r["battery_min"] == 25
    assert [a["action_type"] for a in r["actions"]] == ["coger", "dejar"]
    assert info["instant_actions"] == ["startPause", "stopPause", "cancelOrder", "stateRequest"]


def test_post_order_e_instant_publican_en_el_bus():
    srv, bus = _server()
    c = TestClient(srv.app)
    r = c.post("/api/fleet/order", json={"actionType": "coger", "params": {"target_pos": "H2D2"}})
    assert r.status_code == 200 and r.json()["orderId"].startswith("ui-")
    t, payload = bus.published[-1]
    assert t == "vda5050/v3/imperial_fleet/fleet/order"
    assert payload["manufacturer"] == "imperial_fleet" and payload["serialNumber"] == "fleet"
    act = payload["nodes"][0]["actions"][0]
    assert act["actionType"] == "coger" and act["actionParameters"] == [{"key": "target_pos", "value": "H2D2"}]
    assert c.post("/api/fleet/order", json={"actionType": "volar"}).status_code == 400

    r = c.post("/api/robots/sim-1/instant", json={"actionType": "startPause"})
    assert r.status_code == 200
    t, payload = bus.published[-1]
    assert t == "vda5050/v3/SIM/sim-1/instantActions"
    assert payload["actions"][0]["actionType"] == "startPause"
    assert c.post("/api/robots/nadie/instant", json={"actionType": "startPause"}).status_code == 404
    assert c.post("/api/robots/sim-1/instant", json={"actionType": "volar"}).status_code == 400


def test_websocket_recibe_el_ultimo_estado_al_conectar():
    srv, bus = _server()
    c = TestClient(srv.app)
    st = {**make_header(HeaderCounter(), "SIM", "sim-1", "state"), "orderId": "", "driving": False}
    srv.mirror(topic("SIM", "sim-1", "state"), st)          # publicado antes de que nadie conecte
    srv.mirror(topic("imperial_fleet", "fleet", "order_response"), {"orderId": "x"})   # no se retiene
    with c.websocket_connect("/ws") as ws:
        first = json.loads(ws.receive_text())
        assert first["topic"] == "vda5050/v3/SIM/sim-1/state"
        assert first["payload"]["serialNumber"] == "sim-1"
    assert list(srv.last) == ["vda5050/v3/SIM/sim-1/state"]


# ------------------------------------------------------------ sincronización
class FakeSync:
    """Lo que devuelve `driver.make_sync()`: preview/deploy/catalog sin robot."""

    class Op:
        def __init__(self, guid, verb):
            self.guid, self.verb, self.status = guid, verb, "pendiente"

        def to_dict(self):
            return {"guid": self.guid, "verb": self.verb, "status": self.status}

    class Cat:
        def summary(self):
            return [{"guid": "g1", "kind": "mission", "name": "m"}]

    def __init__(self):
        self.deployed = []
        self.checks = []
        self.lock = threading.Lock()

    def check(self, deep=False):
        self.checks.append(deep)
        return {"level": "completa" if deep else "rápida", "state": {"g1": "ok"}, "required": ["g1"],
                "outside": [], "checked_at": "2026-10-13T12:00:00Z"}

    def catalog(self):
        return self.Cat()

    def preview(self, guids):
        if "nada" in guids:
            raise ValueError("nada no está en el catálogo")
        return None, [self.Op(g, "crear") for g in guids]

    def deploy(self, guids, progress):
        ops = self.preview(guids)[1]
        for op in ops:
            op.status = "hecho"
            progress(op)
        self.deployed.append(guids)
        return ops


def _server_with_sync():
    srv, bus = _server()
    sync = FakeSync()
    srv.robots["sim-1"].driver.make_sync = lambda d: sync
    srv.__init__(srv.cfg, srv.robots, bus)           # vuelve a buscar la capacidad
    return srv, sync


def test_sync_no_aplica_si_el_driver_no_la_ofrece():
    srv, _ = _server()
    c = TestClient(srv.app)
    info = c.get("/api/sync").json()
    assert info == {"catalog": [], "robots": [{"serial": "sim-1", "supported": False}], "reports": {}}
    assert c.post("/api/sync/sim-1/preview", json={"guids": []}).status_code == 400
    assert c.post("/api/sync/nadie/preview", json={"guids": []}).status_code == 404


def test_sync_preview_y_deploy_con_avance_por_websocket():
    srv, sync = _server_with_sync()
    c = TestClient(srv.app)
    info = c.get("/api/sync").json()
    assert info["catalog"][0]["guid"] == "g1" and info["robots"][0]["supported"] is True
    r = c.post("/api/sync/sim-1/preview", json={"guids": ["g1"]})
    assert r.json() == {"ops": [{"guid": "g1", "verb": "crear", "status": "pendiente"}]}
    assert c.post("/api/sync/sim-1/preview", json={"guids": ["nada"]}).status_code == 400

    # Sin telemetría el robot cuenta como "no responde": no se despliega.
    assert c.post("/api/sync/sim-1/deploy", json={"guids": ["g1"]}).status_code == 409
    srv.robots["sim-1"].poll()
    msgs = []
    srv.mirror = lambda t, payload: msgs.append((t, payload))   # lo que iría al WebSocket
    assert c.post("/api/sync/sim-1/deploy", json={"guids": ["g1"]}).json() == {"started": True}
    for _ in range(200):                                        # el despliegue va en su hilo
        if msgs and "report" in msgs[-1][1]:                    # tras desplegar, se comprueba
            break
        time.sleep(0.01)
    assert {t for t, _ in msgs} == {"fm/sync/sim-1"}
    report = msgs.pop()[1]["report"]
    assert report["state"] == {"g1": "ok"} and report["error"] is None
    assert srv.monitor.reports["sim-1"] is report
    msgs = [{"payload": m} for _, m in msgs]
    assert msgs[0]["payload"]["op"]["status"] == "hecho"
    assert msgs[-1]["payload"] == {"done": True, "summary": {"hecho": 1}}
    assert sync.deployed == [["g1"]]


def test_sync_check_bajo_demanda():
    srv, sync = _server_with_sync()
    c = TestClient(srv.app)
    assert c.post("/api/sync/sim-1/check", json={"deep": True}).status_code == 409   # sin telemetría
    srv.robots["sim-1"].poll()
    msgs = []
    srv.mirror = lambda t, payload: msgs.append(payload)
    srv.monitor.publish = lambda s, p: srv.mirror(f"fm/sync/{s}", p)
    assert c.post("/api/sync/sim-1/check", json={"deep": True}).json() == {"started": True}
    for _ in range(200):
        if msgs:
            break
        time.sleep(0.01)
    assert sync.checks == [True] and msgs[0]["report"]["level"] == "completa"
    assert c.get("/api/sync").json()["reports"]["sim-1"]["state"] == {"g1": "ok"}
