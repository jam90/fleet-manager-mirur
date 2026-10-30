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
                                                   action_types=frozenset({"coger", "dejar"}),
                                                   raw={"driver": "sim", "battery": 90,
                                                        "actions": {"coger": {}, "dejar": {}}})},
                      actions={"coger": {"mission": "m"}, "dejar": {}, "volar": {}})
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
            self.guid, self.verb, self.status, self.name = guid, verb, "pendiente", guid

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

    def preview(self, guids, replace=()):
        if "nada" in guids:
            raise ValueError("nada no está en el catálogo")
        return None, [self.Op(g, "corregir" if g.startswith("fix") else "crear") for g in guids]

    def deploy(self, guids, progress, only_create=False, replace=()):
        self.only_create = only_create
        self.replace = list(replace)
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


# ------------------------------------------------------------ configurador
def test_config_solo_lectura():
    srv, _ = _server()
    info = TestClient(srv.app).get("/api/config").json()
    assert [a["name"] for a in info["actions"]] == ["coger", "dejar", "volar"]   # también las sin robot
    assert info["actions"][0] == {"name": "coger", "definition": {"mission": "m"}, "needs": None}
    r = info["robots"][0]
    assert r["actions"] == ["coger", "dejar"] and r["manufacturer"] == "SIM" and r["sync"] is False
    assert r["settings"] == {"battery": 90}        # sin actions, driver… (van en sus campos)
    assert info["auto_charge"]["battery_floor"] == 20
    assert info["assigner"] == {"prefer_not_charging": True}


def test_config_con_lo_que_necesita_cada_action():
    srv, sync = _server_with_sync()
    sync.action_needs = lambda defs: {n: {"required": ["g1"] if d.get("mission") else []} for n, d in defs.items()}
    info = TestClient(srv.app).get("/api/config").json()
    assert info["actions"][0]["needs"] == {"required": ["g1"]} and info["robots"][0]["sync"] is True
    sync.action_needs = lambda defs: 1 / 0          # catálogo roto: la pestaña sigue
    assert TestClient(srv.app).get("/api/config").json()["actions"][0]["needs"] is None


# --------------------------------------------------- configurador: edición
SIM_YAML = """\
actions:
  coger:
  dejar:
robots:
  sim-1:
    driver: sim
    battery_min: 25
    actions: [coger]   # comentario que debe quedarse
"""


def _server_editable(tmp_path):
    from fm.adapters import make_driver
    from fm.config import load_config
    from fm.reload import Reloader
    p = tmp_path / "fleet.yaml"
    p.write_text(SIM_YAML, encoding="utf-8")
    env = tmp_path / "no.env"
    cfg = load_config(p, env)
    robots = {s: Robot(rc, make_driver(rc, cfg), cfg.auto_charge) for s, rc in cfg.robots.items()}
    bus = FakeBus({"sim-1": "SIM"})
    srv = WebServer(cfg, robots, bus, config_path=p, env_path=env)
    notes = []
    srv.reloader = Reloader(cfg, robots, [srv], notify=notes.append)
    return srv, p, notes


def test_config_guardar_y_aplicar(tmp_path):
    srv, p, notes = _server_editable(tmp_path)
    c = TestClient(srv.app)
    info = c.get("/api/config").json()
    assert info["editable"] and info["file"] == {"same": True}
    r = c.post("/api/config", json={"robots": {"sim-1": {"actions": ["coger", "dejar"]}},
                                    "auto_charge": {"battery_floor": 15}}).json()
    assert r["changed"] and r["restart"] == [] and r["backup"].endswith(".bak")
    assert "actions: [coger, dejar]  # comentario que debe quedarse" in p.read_text(encoding="utf-8")
    srv.reloader.apply_pending()                     # lo que hace el hilo principal en el tick
    assert notes == [{"applied": True}]
    info = c.get("/api/config").json()
    assert info["robots"][0]["actions"] == ["coger", "dejar"] and info["auto_charge"]["battery_floor"] == 15
    assert info["file"] == {"same": True}
    # sin cambios / inválida
    assert c.post("/api/config", json={"auto_charge": {"battery_floor": 15}}).json() == {"changed": False}
    bad = c.post("/api/config", json={"robots": {"sim-1": {"actions": ["volar"]}}})
    assert bad.status_code == 400 and "catálogo" in bad.json()["detail"]


def test_config_vuelta_a_la_espera(tmp_path):
    srv, p, notes = _server_editable(tmp_path)
    c = TestClient(srv.app)
    info = c.get("/api/config").json()
    assert info["idle_return"] is None and info["robots"][0]["idle_return"]["mode"] == "fleet"
    assert "idle_return" not in info["robots"][0]["settings"]
    r = c.post("/api/config", json={"idle_return": {"action": "coger", "after_s": 20}}).json()
    assert r["changed"] and r["restart"] == []                       # en caliente
    srv.reloader.apply_pending()
    assert srv.robots["sim-1"].idle.cfg.action == "coger" and srv.robots["sim-1"].idle.cfg.after_s == 20
    # El robot la desactiva; no mandar idle_return de la flota no la quita.
    c.post("/api/config", json={"robots": {"sim-1": {"idle_return": False}}})
    srv.reloader.apply_pending()
    info = c.get("/api/config").json()
    assert info["idle_return"]["action"] == "coger" and info["robots"][0]["idle_return"]["mode"] == "off"
    assert not srv.robots["sim-1"].idle.cfg.enabled
    bad = c.post("/api/config", json={"robots": {"sim-1": {"idle_return": {"action": "dejar"}}}})
    assert bad.status_code == 400 and "no está habilitada en sim-1" in bad.json()["detail"]


def test_config_editada_a_mano(tmp_path):
    srv, p, notes = _server_editable(tmp_path)
    c = TestClient(srv.app)
    p.write_text(SIM_YAML.replace("battery_min: 25", "battery_min: 30"), encoding="utf-8")
    assert c.get("/api/config").json()["file"] == {"same": False, "restart": []}
    assert c.post("/api/config/reload").json() == {"changed": True, "restart": []}
    srv.reloader.apply_pending()
    assert srv.robots["sim-1"].cfg.battery_min == 30
    p.write_text(SIM_YAML.replace("driver: sim", "driver: sim\n    pose: [0, 0, 0]"), encoding="utf-8")
    # battery_min vuelve a 25 (en caliente) y aparece pose (pide reiniciar)
    assert c.get("/api/config").json()["file"]["restart"] == ["robots.sim-1: cambia pose"]


def test_config_sin_edicion_activada():
    srv, _ = _server()
    c = TestClient(srv.app)
    assert c.get("/api/config").json()["editable"] is False
    assert c.post("/api/config", json={}).status_code == 501


def test_config_crear_action_y_habilitarla_a_la_vez(tmp_path):
    srv, p, notes = _server_editable(tmp_path)
    c = TestClient(srv.app)
    r = c.post("/api/config", json={"actions": {"traer": {"definition": {}, "comments": {}}},
                                    "robots": {"sim-1": {"actions": ["coger", "traer"]}}})
    assert r.status_code == 200 and r.json()["restart"] == []
    srv.reloader.apply_pending()
    info = c.get("/api/config").json()
    assert [a["name"] for a in info["actions"]] == ["coger", "dejar", "traer"]
    assert info["robots"][0]["actions"] == ["coger", "traer"]
    # borrarla mientras un robot la tiene: no se guarda
    bad = c.post("/api/config", json={"actions": {"traer": None}})
    assert bad.status_code == 400 and "no están en el catálogo" in bad.json()["detail"]
    # quitarla del robot y borrarla, en la misma edición
    ok = c.post("/api/config", json={"actions": {"traer": None}, "robots": {"sim-1": {"actions": ["coger"]}}})
    assert ok.status_code == 200 and "traer" not in p.read_text(encoding="utf-8")


# ------------------------------------------------------------------- crear
def test_crear_y_capturar(tmp_path):
    from fm.adapters.base import Telemetry
    srv, sync = _server_with_sync()
    calls = []
    sync.create_choices = lambda: {"groups": [{"guid": "G", "name": "g"}], "maps": [],
                                   "created": [{"guid": "M1", "kind": "mission", "name": "x", "robot": "sim-1"}]}
    sync.web_url = lambda g, m=None: f"http://robot/setup/missions/{g}"
    sync.std_guids = lambda kind, parent=None: set()

    def create_mission(name, parent, others, site=None):
        calls.append((name, parent, others, site))
        if name == "repetida":
            raise ValueError("ya hay una mission 'repetida'")
        return {"kind": "mission", "guid": "M2", "name": name, "url": "http://robot/setup/missions/M2"}
    sync.create_mission = create_mission
    sync.create_position = lambda n, p, o, t=0: (calls.append(("pos", n, t)) or
                                                 {"kind": "position", "guid": "P1", "name": n, "url": "u"})
    sync.capture = lambda g: {"kind": "mission", "guid": g, "name": "x", "detail": "3 actions"}
    c = TestClient(srv.app)
    info = c.get("/api/create").json()
    assert info["robots"][0]["can_create"] and info["created"][0]["url"] == "http://robot/setup/missions/M1"
    body = {"name": "nueva", "parent": "G"}
    assert c.post("/api/create/sim-1/mission", json=body).status_code == 409      # sin telemetría
    srv.robots["sim-1"].last_telemetry = Telemetry(battery=80.0)
    r = c.post("/api/create/sim-1/mission", json=body)
    assert r.status_code == 200 and r.json()["guid"] == "M2" and calls[-1] == ("nueva", "G", set(), srv.cfg.site)
    bad = c.post("/api/create/sim-1/mission", json={"name": "repetida", "parent": "G"})
    assert bad.status_code == 400 and "repetida" in bad.json()["detail"]
    assert c.post("/api/create/sim-1/position", json={"name": "Mesa", "parent": "MAP"}).json()["guid"] == "P1"
    assert calls[-1] == ("pos", "Mesa", 0)                                       # sin tipo: Robot position
    c.post("/api/create/sim-1/position", json={"name": "VL", "parent": "MAP", "type_id": 11})
    assert calls[-1] == ("pos", "VL", 11)
    assert c.post("/api/create/sim-1/capture", json={"guid": "M1"}).json()["detail"] == "3 actions"


def test_crear_no_aplica_sin_la_capacidad():
    srv, _ = _server()
    c = TestClient(srv.app)
    assert c.get("/api/create").json()["robots"][0]["can_create"] is False
    assert c.post("/api/create/sim-1/mission", json={"name": "x", "parent": "G"}).status_code == 400



def test_desplegar_con_el_robot_ocupado_solo_si_es_crear():
    from fm.adapters.base import Telemetry
    srv, sync = _server_with_sync()
    srv.robots["sim-1"].last_telemetry = Telemetry(battery=80.0, foreign_busy=True)   # mission de su web
    c = TestClient(srv.app)
    bad = c.post("/api/sync/sim-1/deploy", json={"guids": ["g1", "fix-pos"]})
    assert bad.status_code == 409 and "corregir" in bad.json()["detail"] and "fix-pos" in bad.json()["detail"]
    ok = c.post("/api/sync/sim-1/deploy", json={"guids": ["g1"]})
    assert ok.status_code == 200
    for _ in range(50):
        if sync.deployed:
            break
        time.sleep(0.02)
    assert sync.deployed == [["g1"]] and sync.only_create is True


def test_borrar_desde_la_web():
    from fm.adapters.base import Telemetry
    srv, sync = _server_with_sync()
    store = {"M1": True}
    sync.describe = lambda g: {"guid": g, "kind": "mission", "name": "prueba"} if g == "M1" else None
    sync.delete_blockers = lambda g, defs, wanted: []
    sync.exists = lambda g: store.get(g, False)
    sync.delete = lambda g: store.pop(g)
    sync.forget = lambda g, retire=False, robots=(): [g]
    c = TestClient(srv.app)
    assert c.post("/api/create/delete/preview", json={"guid": "X"}).status_code == 404
    plan = c.post("/api/create/delete/preview", json={"guid": "M1"}).json()
    assert plan["blockers"] == ["sim-1 no responde: podría tener una copia"]
    assert c.post("/api/create/delete", json={"guid": "M1"}).status_code == 409 and store
    srv.robots["sim-1"].last_telemetry = Telemetry(battery=80.0, foreign_busy=True)
    assert c.post("/api/create/delete/preview", json={"guid": "M1"}).json()["blockers"] == \
        ["sim-1 está ocupado: espera a que quede libre"]
    srv.robots["sim-1"].last_telemetry = Telemetry(battery=80.0)
    out = c.post("/api/create/delete", json={"guid": "M1"}).json()
    assert out["robots"] == ["sim-1"] and out["files"] == 1 and not store


def test_crear_grupo_y_no_borrarlo_con_missions_en_el_robot():
    from fm.adapters.base import Telemetry
    srv, sync = _server_with_sync()
    srv.robots["sim-1"].last_telemetry = Telemetry(battery=80.0)
    sync.std_guids = lambda kind, parent=None: set()
    sync.create_group = lambda name, others: {"kind": "missiongrp", "guid": "G2", "name": name}
    c = TestClient(srv.app)
    assert c.post("/api/create/group", json={"name": "pruebas"}).json()["guid"] == "G2"
    # Sin catálogo que lo vea, el robot tiene una mission hecha en su web dentro.
    sync.describe = lambda g: {"guid": g, "kind": "missiongrp", "name": "pruebas"}
    sync.delete_blockers = lambda g, defs, wanted: []
    sync.exists = lambda g: True
    sync.in_use_here = lambda g: ["el grupo tiene missions: a mano"]
    assert c.post("/api/create/delete/preview", json={"guid": "G2"}).json()["blockers"] == \
        ["sim-1: el grupo tiene missions: a mano"]


def test_retirar_y_quitar_desde_la_web():
    from fm.adapters.base import Telemetry
    srv, sync = _server_with_sync()
    srv.robots["sim-1"].last_telemetry = Telemetry(battery=80.0)
    store, forgot = {"M1": True}, []
    sync.catalog_item = lambda g: {"guid": g, "kind": "mission", "name": "vieja"} if g == "M1" else None
    sync.retire_blockers = lambda g, defs, wanted: []
    sync.remove_blockers = lambda g: [] if store.get(g) else ["este robot no la tiene"]
    sync.exists = lambda g: store.get(g, False)
    sync.delete = lambda g: store.pop(g)
    sync.forget = lambda g, retire=False, robots=(): forgot.append((g, retire, list(robots))) or [g]
    c = TestClient(srv.app)
    assert c.post("/api/sync/sim-1/remove/preview", json={"guid": "M1"}).json()["blockers"] == []
    assert c.post("/api/sync/sim-1/remove", json={"guid": "M1"}).json()["name"] == "vieja"
    assert not store and not forgot                                    # quitar no toca el catálogo
    bad = c.post("/api/sync/sim-1/remove", json={"guid": "M1"})
    assert bad.status_code == 409 and "no la tiene" in bad.json()["detail"]
    store["M1"] = True
    plan = c.post("/api/retire/preview", json={"guid": "M1"}).json()
    assert plan["robots"] == ["sim-1"] and plan["blockers"] == []
    assert c.post("/api/retire", json={"guid": "M1"}).status_code == 200
    assert forgot == [("M1", True, ["sim-1"])] and not store            # retirar: catálogo + reserva


# --------------------------------------------- altas/bajas de robots y reinicio (v3)
def test_alta_de_robot_pide_reiniciar_y_reinicio(tmp_path):
    srv, p, notes = _server_editable(tmp_path)
    c = TestClient(srv.app)
    info = c.get("/api/config").json()
    assert info["can_restart"] is False and "sim" in info["drivers_available"] and "mir" in info["drivers_available"]
    assert info["robots"][0]["busy"] is False and info["robots"][0]["order"] is None
    r = c.post("/api/config", json={"robots": {"sim-2": {"driver": "sim", "battery_min": 30, "actions": ["coger"]}}})
    assert r.status_code == 200 and r.json()["restart"] == ["robots nuevos: sim-2"]
    assert "  sim-2:\n    driver: sim\n    battery_min: 30\n    actions: [coger]\n" in p.read_text(encoding="utf-8")
    assert c.get("/api/config").json()["file"] == {"same": False, "restart": ["robots nuevos: sim-2"]}
    assert c.post("/api/restart").status_code == 501
    asked = []
    srv.on_restart = lambda: asked.append(True)
    assert c.get("/api/config").json()["can_restart"] is True
    assert c.post("/api/restart").json() == {"restarting": True} and asked == [True]


def test_baja_de_robot_por_la_api(tmp_path):
    srv, p, notes = _server_editable(tmp_path)
    p.write_text(SIM_YAML + "  sim-2:\n    driver: sim\n    actions: []\n", encoding="utf-8")
    r = TestClient(srv.app).post("/api/config", json={"robots": {"sim-2": None}})
    assert r.status_code == 200 and "sim-2" not in p.read_text(encoding="utf-8")


def test_misiones_del_robot():
    from fm.adapters.base import MissionRun, MissionsView
    srv, _ = _server()
    c = TestClient(srv.app)
    # El sim no ofrece misiones: la pestaña lo dice, no falla
    assert c.get("/api/robots/sim-1/missions").json() == {"supported": False}
    assert c.get("/api/robots/nadie/missions").status_code == 404

    class Src:
        fail = False

        def view(self, history=10):
            if self.fail:
                raise ConnectionError("sin red")
            return MissionsView(None, [], [MissionRun("7", "coger", "FINISHED", own=True)])

    src = Src()
    srv.robots["sim-1"].driver.make_missions = lambda: src
    r = c.get("/api/robots/sim-1/missions").json()
    assert r["supported"] and r["current"] is None
    assert r["history"][0]["name"] == "coger" and r["history"][0]["own"]
    # Robot apagado: aviso en el JSON, no un 500
    src.fail = True
    r = c.get("/api/robots/sim-1/missions").json()
    assert r["supported"] and "sin red" in r["error"]


def test_site_de_la_flota_desde_la_cabecera(tmp_path):
    # Decisión 129: se guarda en fleet.yaml, se aplica en caliente y otra
    # edición que no lo manda no lo quita.
    srv, p, notes = _server_editable(tmp_path)
    c = TestClient(srv.app)
    assert c.get("/api/fleet").json()["site"] is None
    r = c.post("/api/config", json={"site": {"guid": "imperialfleet-session000-01-0000-001", "name": "nave"}}).json()
    assert r["changed"] and r["restart"] == []
    srv.reloader.apply_pending()
    assert srv.robots["sim-1"].site == "imperialfleet-session000-01-0000-001"
    assert c.get("/api/fleet").json()["site"] == "imperialfleet-session000-01-0000-001"
    c.post("/api/config", json={"auto_charge": {"battery_floor": 15}})
    assert "site: imperialfleet-session000-01-0000-001   # nave" in p.read_text(encoding="utf-8")
    c.post("/api/config", json={"site": None})
    srv.reloader.apply_pending()
    assert srv.robots["sim-1"].site is None and "site:" not in p.read_text(encoding="utf-8")


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


def test_seguimiento_de_una_fleet_order_en_cola():
    # Cola del FM (decisión 154): QUEUED no es un rechazo, y un CANCEL_FAILED
    # no toca una order que sigue en marcha.
    srv, bus = _server()
    c = TestClient(srv.app)
    srv.mirror(RESP, _verdict("mes-3", "QUEUED", queuePosition=2, description="sim-1: ocupado"))
    o = c.get("/api/orders/mes-3").json()
    assert o["status"] == "QUEUED" and o["queuePosition"] == 2 and o["description"] == "sim-1: ocupado"
    srv.mirror(RESP, _verdict("mes-3", assignedSerial="sim-1"))
    o = c.get("/api/orders/mes-3").json()
    assert o["status"] == "ASSIGNED" and "queuePosition" not in o
    srv.mirror(RESP, _verdict("mes-3", "CANCEL_FAILED", errorType="NO_ORDER_TO_CANCEL"))
    assert c.get("/api/orders/mes-3").json()["status"] == "ASSIGNED"

    for status in ("CANCELLED", "EXPIRED"):
        srv.mirror(RESP, _verdict("mes-4", "QUEUED", queuePosition=1))
        srv.mirror(RESP, _verdict("mes-4", status, description="fuera de la cola"))
        assert c.get("/api/orders/mes-4").json()["status"] == status
    srv.mirror(RESP, _verdict("mes-5", "FAILED", errorType="ROBOT_UNREACHABLE", errorDescription="sin red"))
    o = c.get("/api/orders/mes-5").json()
    assert o["status"] == "FAILED" and o["errorType"] == "ROBOT_UNREACHABLE"


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


# ------------------------------------------- cola de fleet/order (decisión 156)
def test_cancelar_una_fleet_order_desde_la_api():
    srv, bus = _server()
    c = TestClient(srv.app)
    assert c.post("/api/orders/nada/cancel").status_code == 404
    srv.mirror(RESP, _verdict("q-1", "QUEUED", queuePosition=1))
    publish = bus.publish
    answer = {}

    def publish_and_answer(serial, subtopic, body, retain=False):
        # El "Dispatcher" de mentira contesta al cancelOrder de la flota.
        out = publish(serial, subtopic, body, retain)
        if subtopic == "instantActions" and serial == "fleet":
            oid = body["actions"][0]["actionParameters"][0]["value"]
            threading.Thread(target=srv.mirror, args=(RESP, _verdict(oid, **answer))).start()
        return out

    bus.publish = publish_and_answer
    answer.update(status="CANCEL_FAILED", errorType="NO_ORDER_TO_CANCEL", errorDescription="no está")
    r = c.post("/api/orders/q-1/cancel?wait=2")
    assert r.status_code == 409 and "no está" in r.json()["detail"]
    t, msg = bus.published[-1]
    assert t == "vda5050/v3/imperial_fleet/fleet/instantActions"
    assert msg["actions"][0]["actionType"] == "cancelOrder"
    assert c.get("/api/orders/q-1").json()["status"] == "QUEUED"     # un fallo no la toca

    answer.update(status="CANCELLED", description="cancelada por cancelOrder")
    r = c.post("/api/orders/q-1/cancel?wait=2")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert c.post("/api/orders/q-1/cancel").status_code == 409             # ya terminó

    bus.publish = publish                                                # nadie contesta
    srv.mirror(RESP, _verdict("q-2", "QUEUED", queuePosition=1))
    assert c.post("/api/orders/q-2/cancel?wait=0.1").status_code == 202
    assert c.post("/api/orders/q-2/cancel").status_code == 202

    srv.send_robot_order("sim-1", "coger", {}, "d-9")                      # dirigida: por su robot
    assert "instant" in c.post("/api/orders/d-9/cancel").json()["detail"]


def test_cancelada_en_curso_no_pasa_a_failed():
    # El cancelOrder del robot deja la action FAILED en su state; la order
    # cancelada desde la flota tiene que seguir CANCELLED.
    srv, bus = _server()
    c = TestClient(srv.app)
    srv.mirror(RESP, _verdict("q-3", assignedSerial="sim-1"))
    srv.mirror(RESP, _verdict("q-3", "CANCELLED", description="cancelada"))
    srv.mirror(ST, _state("q-3", [{"actionId": "a", "actionType": "coger", "actionStatus": "FAILED"}]))
    assert c.get("/api/orders/q-3").json()["status"] == "CANCELLED"


def test_cola_desde_el_espejo():
    srv, bus = _server()
    c = TestClient(srv.app)
    assert c.get("/api/queue").status_code == 503
    tasks = [{"orderId": "q-1", "orderUpdateId": 0, "actionType": "coger",
              "receivedAt": "2026-10-30T11:01:21.918Z", "waiting": "sim-1: ocupado"}]
    srv.mirror("vda5050/v3/imperial_fleet/fleet/queue", {"tasks": tasks})
    assert c.get("/api/queue").json() == tasks
