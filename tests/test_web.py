"""Servidor web: snapshot, envío (publica en el bus) y espejo por WebSocket."""
import json
import threading

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


# ---------------------------------------------------------------- API REST
def _state(order_id="", action_states=(), errors=(), battery=80.0):
    return {**make_header(HeaderCounter(), "SIM", "sim-1", "state"), "orderId": order_id,
            "orderUpdateId": 0, "driving": bool(order_id), "paused": False, "operatingMode": "AUTOMATIC",
            "powerSupply": {"stateOfCharge": battery, "charging": False},
            "mobileRobotPosition": {"x": 1.0, "y": 2.0, "theta": 0.0, "mapId": "m", "localized": True},
            "actionStates": list(action_states), "errors": list(errors), "information": []}


def _verdict(order_id, status="ASSIGNED", **extra):
    return {"orderId": order_id, "orderUpdateId": 0, "status": status, **extra}


ST = "vda5050/v3/SIM/sim-1/state"
RESP = "vda5050/v3/imperial_fleet/fleet/order_response"


def test_estado_de_robots_desde_el_espejo():
    srv, bus = _server()
    c = TestClient(srv.app)
    assert c.get("/api/robots/sim-1/state").status_code == 503      # aún no se ha publicado
    assert c.get("/api/robots/nadie").status_code == 404
    srv.mirror(ST, _state(battery=55.5))
    srv.mirror("vda5050/v3/SIM/sim-1/connection", {"connectionState": "ONLINE"})
    srv.mirror("vda5050/v3/SIM/sim-1/factsheet", {"typeSpecification": {"seriesName": "sim"}})

    r = c.get("/api/robots").json()[0]
    assert r["serial"] == "sim-1" and r["battery"] == 55.5 and r["connection"] == "ONLINE"
    assert r["reachable"] is True and r["position"]["x"] == 1.0
    assert c.get("/api/robots/sim-1").json()["battery"] == 55.5
    assert c.get("/api/robots/sim-1/state").json()["powerSupply"]["stateOfCharge"] == 55.5
    assert c.get("/api/robots/sim-1/connection").json()["connectionState"] == "ONLINE"
    assert c.get("/api/robots/sim-1/factsheet").json()["typeSpecification"]["seriesName"] == "sim"
    assert [a["action_type"] for a in c.get("/api/robots/sim-1/actions").json()] == ["coger", "dejar"]
    assert c.get("/api/health").json()["ok"] is True

    srv.mirror(ST, _state(errors=[{"errorType": "ROBOT_UNREACHABLE", "errorLevel": "WARNING"}]))
    h = c.get("/api/health").json()
    assert h["ok"] is False and h["robots"]["sim-1"]["reachable"] is False


def test_seguimiento_de_una_fleet_order():
    srv, bus = _server()
    c = TestClient(srv.app)
    r = c.post("/api/fleet/order", json={"actionType": "coger", "orderId": "mes-1"})
    assert r.status_code == 200 and r.json()["orderId"] == "mes-1" and r.json()["status"] == "PENDING"
    assert bus.published[-1][1]["orderId"] == "mes-1"         # el id del cliente llega al broker

    srv.mirror(RESP, _verdict("mes-1", assignedSerial="sim-1"))
    o = c.get("/api/orders/mes-1").json()
    assert o["status"] == "ASSIGNED" and o["serial"] == "sim-1" and o["target"] == "fleet"

    srv.mirror(ST, _state("mes-1", [{"actionId": "a", "actionType": "coger", "actionStatus": "RUNNING"}]))
    assert c.get("/api/orders/mes-1").json()["status"] == "RUNNING"
    srv.mirror(ST, _state("mes-1", [{"actionId": "a", "actionType": "coger", "actionStatus": "FINISHED"}]))
    assert c.get("/api/orders/mes-1").json()["status"] == "FINISHED"

    srv.mirror(RESP, _verdict("mes-2", "REJECTED", errorType="NO_MOBILE_ROBOT_AVAILABLE",
                              errorDescription="sim-1: ocupado"))
    o = c.get("/api/orders/mes-2").json()
    assert o["status"] == "REJECTED" and o["description"] == "sim-1: ocupado"
    assert [x["orderId"] for x in c.get("/api/orders").json()] == ["mes-2", "mes-1"]
    assert c.get("/api/orders/nada").status_code == 404


def test_order_dirigida():
    srv, bus = _server()
    c = TestClient(srv.app)
    r = c.post("/api/robots/sim-1/order", json={"actionType": "dejar", "params": {"p": 1}, "orderId": "d-1"})
    assert r.status_code == 200
    t, payload = bus.published[-1]
    assert t == "vda5050/v3/SIM/sim-1/order"
    assert payload["manufacturer"] == "SIM" and payload["serialNumber"] == "sim-1"
    assert payload["nodes"][0]["actions"][0]["actionParameters"] == [{"key": "p", "value": 1}]
    assert c.post("/api/robots/sim-1/order", json={"actionType": "volar"}).status_code == 400
    assert c.post("/api/robots/nadie/order", json={"actionType": "dejar"}).status_code == 404

    # Rechazo: el state sigue sin esa order y el motivo va a errors[] con su orderId.
    err = {"errorType": "OTHER_ORDER_ACTIVE", "errorLevel": "WARNING", "errorDescription": "ocupado",
           "errorReferences": [{"referenceKey": "orderId", "referenceValue": "d-1"}]}
    srv.mirror(ST, _state("otra", errors=[err]))
    o = c.get("/api/orders/d-1").json()
    assert o["status"] == "REJECTED" and o["errorType"] == "OTHER_ORDER_ACTIVE" and o["target"] == "robot"


def test_wait_devuelve_el_veredicto_o_202():
    srv, bus = _server()
    c = TestClient(srv.app)
    # El "Dispatcher" de mentira contesta en cuanto se publica la order.
    publish = bus.publish

    def publish_and_answer(serial, subtopic, body, retain=False):
        out = publish(serial, subtopic, body, retain)
        if subtopic == "order" and serial == "fleet":
            threading.Thread(target=srv.mirror, args=(RESP, _verdict(body["orderId"], assignedSerial="sim-1"))).start()
        return out

    bus.publish = publish_and_answer
    r = c.post("/api/fleet/order?wait=2", json={"actionType": "coger"})
    assert r.status_code == 200 and r.json()["status"] == "ASSIGNED" and r.json()["serial"] == "sim-1"

    bus.publish = publish                                       # nadie contesta
    r = c.post("/api/fleet/order?wait=0.1", json={"actionType": "coger"})
    assert r.status_code == 202 and r.json()["status"] == "PENDING"
    assert c.post("/api/fleet/order?wait=99", json={"actionType": "coger"}).status_code == 422
