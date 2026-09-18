"""Servidor web: snapshot, envío (publica en el bus) y espejo por WebSocket."""
import json

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
