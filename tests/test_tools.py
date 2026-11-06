"""Herramientas de los robots (decisión 184): catálogo `tools:`, cambiador de
cada robot, la que lleva montada (la lee el driver) y cómo eligen robot el
asignador, la cola y las orders dirigidas. Con robots simulados, sin red."""
import json

import pytest

from fm.adapters import make_driver
from fm.assigner import RobotSnapshot, assign
from fm.config import ConfigError, load_config
from fm.fleet import Dispatcher
from fm.reload import restart_reasons
from fm.robot import Robot
from fm.tools import changer_notice, needed_tool, tool_name, tool_problem
from fm.vda5050.header import HeaderCounter, make_header, topic
from fm.vda5050.order import Action, ActionParameter, OrderRejected
from test_fleet import FLEET_MAN, FakeBus

YAML = """
tools:
  vgc10: {code: 1, description: "Ventosa VGC10"}
  2fg7: {code: 2}
actions:
  coger: {mission: coger, tool_input: garra}
  ir_a: {mission: ir}
robots:
  sim-1: {driver: sim, tool: vgc10, tool_changer: [vgc10, 2fg7], actions: [coger, ir_a]}
  sim-2: {driver: sim, tool: 2fg7, actions: [coger, ir_a]}
"""


def _cfg(tmp_path, text=YAML):
    p = tmp_path / "fleet.yaml"
    p.write_text(text, encoding="utf-8")
    return load_config(p, env_path=tmp_path / "no.env")


def _fleet(tmp_path, text=YAML):
    cfg = _cfg(tmp_path, text)
    robots = {s: Robot(rc, make_driver(rc, cfg, env={})) for s, rc in cfg.robots.items()}
    for r in robots.values():
        r.poll()
    bus = FakeBus({s: "SIM" for s in robots})
    return Dispatcher(cfg, robots, bus, lambda r: None), robots, bus


def _order(order_id, garra=None, action="coger", serial="fleet", man=FLEET_MAN):
    params = [] if garra is None else [{"key": "garra", "value": garra}]
    return json.dumps({**make_header(HeaderCounter(), man, serial, "order"),
                       "orderId": order_id, "orderUpdateId": 0, "edges": [],
                       "nodes": [{"nodeId": "N0", "sequenceId": 0, "released": True,
                                  "actions": [{"actionType": action, "actionId": "a1", "blockingType": "HARD",
                                               "actionParameters": params}]}]})


def _send(disp, order_id, garra=None, action="coger"):
    disp.handle(topic(FLEET_MAN, "fleet", "order"), _order(order_id, garra, action))


def _resp(bus, order_id):
    return [p for t, p in bus.published if t.endswith("/order_response") and p["orderId"] == order_id][-1]


# ---------------------------------------------------------------- config
def test_config_catalogo_cambiador_y_tool_input(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.tools == {"vgc10": {"code": 1, "description": "Ventosa VGC10"}, "2fg7": {"code": 2, "description": ""}}
    r1, r2 = cfg.robots["sim-1"], cfg.robots["sim-2"]
    assert r1.tool_changer == {"vgc10", "2fg7"} and r2.tool_changer == frozenset()
    assert r1.tool_inputs == {"coger": "garra"}
    assert r1.raw["tool_codes"] == {"vgc10": 1, "2fg7": 2}      # para el driver: leer y escribir el código


@pytest.mark.parametrize("bad, msg", [
    ("tools: {x: {description: a}}", "falta 'code'"),
    ("tools: {x: {code: 0}}", "mayor que 0"),
    ("tools: {x: {code: 1}, y: {code: 1}}", "ya es el de 'x'"),
    ("tools: {x: {code: 1, color: rojo}}", "claves desconocidas"),
    ("tools: {x: {code: 1}}\nrobots: {sim-1: {driver: sim, tool_changer: [y]}}", "no están en el catálogo 'tools:'"),
    ("actions: {coger: {mission: c, tool_input: garra}}", "no hay catálogo 'tools:'"),
])
def test_config_errores(tmp_path, bad, msg):
    with pytest.raises(ConfigError, match=msg):
        _cfg(tmp_path, bad)


# --------------------------------------------------- funciones puras (fm/tools.py)
def test_needed_tool():
    actions, tools = {"coger": {"tool_input": "garra"}, "ir_a": {}}, {"vgc10": {}}
    assert needed_tool(Action("ir_a", "a"), actions, tools) is None
    assert needed_tool(Action("coger", "a", actionParameters=[ActionParameter("garra", "vgc10")]), actions, tools) == "vgc10"
    with pytest.raises(OrderRejected, match="falta el parámetro obligatorio 'garra'"):
        needed_tool(Action("coger", "a"), actions, tools)
    with pytest.raises(OrderRejected, match="herramienta 'pinza' desconocida"):
        needed_tool(Action("coger", "a", actionParameters=[ActionParameter("garra", "pinza")]), actions, tools)


def test_tool_problem_y_nombres():
    assert tool_problem("a", "a", ()) == "" and tool_problem("a", "b", {"a"}) == ""
    assert tool_problem("a", "b", {"c"}) == "lleva b y no tiene a en su cambiador"
    assert tool_problem("a", "b", ()) == "lleva b, no a (sin cambiador)"
    assert tool_problem("a", "", ()) == "no lleva herramienta, no a (sin cambiador)"
    assert "no se sabe" in tool_problem("a", None, ())
    codes = {"vgc10": 1}
    assert (tool_name(1, codes), tool_name(0, codes), tool_name(7, codes)) == ("vgc10", "", "código 7")
    assert changer_notice({"b", "a"}, "a") == "revisa que su cambiador tiene a, b (lo dice fleet.yaml); ahora lleva a"


# ------------------------------------------------------------ asignador
def test_asignador_filtra_y_prefiere_la_que_ya_lleva(tmp_path):
    cfg = _cfg(tmp_path)
    snaps = {"sim-1": RobotSnapshot("sim-1", 90, False, tool="vgc10"),
             "sim-2": RobotSnapshot("sim-2", 50, False, tool="2fg7")}
    # 2fg7: valen los dos (sim-1 la tiene en el cambiador), pero va el que ya la lleva aunque tenga menos batería.
    r = assign("coger", snaps, cfg, "2fg7")
    assert r.serial == "sim-2" and r.ranking == ["sim-2", "sim-1"]
    # vgc10: sim-2 no la lleva ni tiene cambiador.
    r = assign("coger", snaps, cfg, "vgc10")
    assert r.serial == "sim-1" and r.rejections == {"sim-2": "lleva 2fg7, no vgc10 (sin cambiador)"}
    # Sin herramienta pedida, como siempre: más batería.
    assert assign("ir_a", snaps, cfg).serial == "sim-1"


# -------------------------------------------- cola de fleet/order y robots simulados
def test_fleet_order_va_al_que_puede_y_la_mission_la_cambia(tmp_path):
    disp, robots, bus = _fleet(tmp_path)
    _send(disp, "o1", "2fg7")
    assert _resp(bus, "o1")["assignedSerial"] == "sim-2"            # ya la lleva
    _send(disp, "o2", "2fg7")                                        # sim-2 ocupado: sim-1 se la cambia
    assert _resp(bus, "o2")["assignedSerial"] == "sim-1"
    robots["sim-1"].poll()
    assert robots["sim-1"].snapshot().tool == "2fg7"
    # La que lleva sale en el `state` VDA (information[], decisión 184): la web la pinta.
    state = robots["sim-1"].state(make_header(HeaderCounter(), "SIM", "sim-1", "state"))
    assert [i.infoDescriptor for i in state.information if i.infoType == "TOOL"] == ["2fg7"]


def test_fleet_order_espera_al_unico_que_puede(tmp_path):
    disp, robots, bus = _fleet(tmp_path)
    _send(disp, "o1", "vgc10")                                       # sim-1 (la lleva)
    _send(disp, "o2", "vgc10")                                       # solo sim-1 puede: a la cola
    r = _resp(bus, "o2")
    assert r["status"] == "QUEUED" and "sim-2: lleva 2fg7, no vgc10 (sin cambiador)" in r["description"]


def test_fleet_order_rechazada_si_falta_o_nadie_puede(tmp_path):
    disp, robots, bus = _fleet(tmp_path, YAML.replace("  2fg7: {code: 2}", "  2fg7: {code: 2}\n  3fg15: {code: 3}"))
    _send(disp, "o1")                                                # sin garra
    assert _resp(bus, "o1")["errorType"] == "VALIDATION_FAILURE"
    _send(disp, "o2", "pinza")                                       # no está en el catálogo
    assert _resp(bus, "o2")["errorType"] == "VALIDATION_FAILURE"
    _send(disp, "o3", "3fg15")                                       # nadie la lleva ni la tiene en su cambiador
    r = _resp(bus, "o3")
    assert r["status"] == "REJECTED" and r["errorType"] == "TOOL_NOT_AVAILABLE"
    assert "sim-1: lleva vgc10 y no tiene 3fg15 en su cambiador" in r["errorDescription"]


def test_order_dirigida_a_un_robot_sin_esa_herramienta(tmp_path):
    disp, robots, bus = _fleet(tmp_path)
    disp.handle(topic("SIM", "sim-2", "order"), _order("d1", "vgc10", serial="sim-2", man="SIM"))
    errors = [e for e in robots["sim-2"].orders.order_errors if e.errorType == "TOOL_NOT_AVAILABLE"]
    assert errors and "lleva 2fg7, no vgc10" in errors[0].errorDescription
    assert robots["sim-2"].driver._jobs == {}                        # no se ha lanzado nada


def test_cambiar_herramientas_se_aplica_en_caliente(tmp_path):
    old = _cfg(tmp_path)
    robots = {s: Robot(rc, make_driver(rc, old, env={})) for s, rc in old.robots.items()}
    new = _cfg(tmp_path, YAML.replace("tool_changer: [vgc10, 2fg7]", "tool_changer: [vgc10]")
                         .replace("  2fg7: {code: 2}", "  2fg7: {code: 2}\n  3fg15: {code: 3}"))
    assert restart_reasons(old, new, robots) == []


def test_factsheet_describe_el_parametro_de_herramienta(tmp_path):
    from fm.vda5050.factsheet import _action
    cfg = _cfg(tmp_path)
    d = make_driver(cfg.robots["sim-1"], cfg, env={})
    coger = next(a for a in d.describe_actions() if a.action_type == "coger")
    assert _action(coger)["actionParameters"] == [{"key": "garra", "valueDataType": "STRING", "isOptional": False,
                                                   "description": "herramienta: 2fg7, vgc10"}]
