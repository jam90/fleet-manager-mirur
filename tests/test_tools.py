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
from fm.tools import (changer_notice, needed_tools, parse_tools, pick_tool, stock_problem, tool_holders,
                      tool_name, tool_problem, with_tool)
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
    # Decisión 212: con qué herramientas se hace un tipo de tarea.
    ("tools: {x: {code: 1}}\nactions: {coger: {mission: c, tools: [x]}}", "hace falta 'tool_input'"),
    ("tools: {x: {code: 1}}\nactions: {coger: {mission: c, tool_input: g, tools: [y]}}", "y no están en el catálogo"),
    ("tools: {x: {code: 1}}\nactions: {coger: {mission: c, tool_input: g, tools: x}}", "debe ser una lista"),
    ("tools: {x: {code: 1}}\nactions: {coger: {mission: c, tool_input: g, tools: []}}", "debe ser una lista"),
    ("tools: {x: {code: 1}}\nactions: {coger: {mission: c, tool_input: g, tools: [x, x]}}", "repetidas"),
])
def test_config_errores(tmp_path, bad, msg):
    with pytest.raises(ConfigError, match=msg):
        _cfg(tmp_path, bad)


# --------------------------------------------------- funciones puras (fm/tools.py)
def _coger(garra):
    return Action("coger", "a", actionParameters=[ActionParameter("garra", garra), ActionParameter("pieza", 3)])


def test_needed_tools():
    actions, tools = {"coger": {"tool_input": "garra"}, "ir_a": {}}, {"vgc10": {}, "2fg7": {}}
    assert needed_tools(Action("ir_a", "a"), actions, tools) is None
    assert needed_tools(_coger("vgc10"), actions, tools) == ["vgc10"]
    # Varias (decisión 211): con comas o como lista JSON; el orden es la preferencia.
    assert needed_tools(_coger(" 2fg7, vgc10 ,2fg7"), actions, tools) == ["2fg7", "vgc10"]
    assert needed_tools(_coger(["vgc10", "2fg7"]), actions, tools) == ["vgc10", "2fg7"]
    for falta in (Action("coger", "a"), _coger(""), _coger(" , ")):
        with pytest.raises(OrderRejected, match="falta el parámetro obligatorio 'garra'"):
            needed_tools(falta, actions, tools)
    with pytest.raises(OrderRejected, match="herramienta 'pinza' desconocida"):
        needed_tools(_coger("vgc10,pinza"), actions, tools)


def test_herramientas_del_tipo_de_tarea():
    """Decisión 212: `tools` en el tipo de tarea. Sin parámetro valen esas, en su
    orden; con él, solo se puede acotar dentro de ellas."""
    actions = {"coger": {"tool_input": "garra", "tools": ["vgc10", "2fg7"]}}
    tools = {"vgc10": {}, "2fg7": {}, "rq140": {}}
    assert needed_tools(Action("coger", "a"), actions, tools) == ["vgc10", "2fg7"]
    assert needed_tools(_coger(""), actions, tools) == ["vgc10", "2fg7"]
    assert needed_tools(_coger("2fg7"), actions, tools) == ["2fg7"]
    with pytest.raises(OrderRejected, match="rq140 no vale para 'coger' \\(valen vgc10, 2fg7\\)"):
        needed_tools(_coger("2fg7,rq140"), actions, tools)
    # Al driver le llega con el parámetro aunque la order no lo trajera.
    assert with_tool(Action("coger", "a"), "garra", "vgc10").params() == {"garra": "vgc10"}


def test_varias_herramientas_elegir_una():
    assert parse_tools("a") == ["a"] and parse_tools(["a", "b", "a"]) == ["a", "b"]
    # La que lleva, si es de la lista, aunque no sea la primera.
    assert pick_tool(["a", "b"], "b", {"a", "b"}) == ("b", "")
    # Si no, la primera de la lista que tenga en su cambiador.
    assert pick_tool(["a", "b", "c"], "", {"c", "b"}) == ("b", "")
    # La primera no queda en el soporte: la siguiente.
    busy = {"a": "no queda a libre en el soporte (ocupada por x)"}
    assert pick_tool(["a", "b"], "", {"a", "b"}, lambda t: busy.get(t, "")) == ("b", "")
    assert pick_tool(["a"], "", {"a", "b"}, lambda t: busy.get(t, "")) == (None, busy["a"])
    # Ninguna: el motivo las nombra todas.
    assert tool_problem(["a", "c"], "b", {"d"}) == "lleva b y no tiene a ni c en su cambiador"
    assert tool_problem(["a", "c"], "b", ()) == "lleva b, no a ni c (sin cambiador)"
    assert pick_tool(["a", "c"], "b", ()) == (None, "lleva b, no a ni c (sin cambiador)")
    # Al driver le llega una sola; lo demás de la action, igual.
    one = with_tool(_coger("vgc10,2fg7"), "garra", "2fg7")
    assert one.params() == {"garra": "2fg7", "pieza": 3} and _coger("x").params()["garra"] == "x"


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


def test_asignador_con_varias_herramientas(tmp_path):
    cfg = _cfg(tmp_path)
    snaps = {"sim-1": RobotSnapshot("sim-1", 50, False, tool=""),
             "sim-2": RobotSnapshot("sim-2", 40, False, tool="2fg7")}
    # Valen los dos; va el que ya lleva una de la lista, y cada uno con la suya.
    r = assign("coger", snaps, cfg, ["vgc10", "2fg7"])
    assert r.ranking == ["sim-2", "sim-1"] and r.tools == {"sim-2": "2fg7", "sim-1": "vgc10"}
    # Una sola de la lista no la puede usar sim-2 (sin cambiador): fuera, con el motivo.
    r = assign("coger", snaps, cfg, ["vgc10"])
    assert r.ranking == ["sim-1"] and r.rejections["sim-2"] == "lleva 2fg7, no vgc10 (sin cambiador)"


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


def test_la_cola_dice_que_herramienta_pide_y_que_espera(tmp_path):
    # Revisión de la interfaz (2026-11-07): en Operación se ve la garra de cada tarea,
    # y el motivo de la espera no suena a rechazo.
    disp, robots, bus = _fleet(tmp_path)
    _send(disp, "o1", "vgc10")
    _send(disp, "o2", "vgc10")
    disp.tick()
    queue = next(p for t, p in reversed(bus.published) if t.endswith("/fleet/queue"))["tasks"]
    assert [(t["orderId"], t["tool"]) for t in queue] == [("o2", "vgc10")]
    assert queue[0]["waiting"].startswith("ahora no puede ningún robot: ")
    assert "ningún robot puede atender" not in _resp(bus, "o2")["description"]
    # La tarea en curso dice en el `state` qué herramienta pide (TOOL_REQUESTED).
    state = robots["sim-1"].state(make_header(HeaderCounter(), "SIM", "sim-1", "state"))
    assert [i.infoDescriptor for i in state.information if i.infoType == "TOOL_REQUESTED"] == ["vgc10"]
    idle = robots["sim-2"].state(make_header(HeaderCounter(), "SIM", "sim-2", "state"))
    assert not [i for i in idle.information if i.infoType == "TOOL_REQUESTED"]


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


# ------------------------------------------- unidades en el soporte (decisión 196)
# Tres robots con acoplador y un soporte en la mesa con UNA ventosa; sim-3 lleva
# fija una pinza de otra marca (sin acoplador), que no es del soporte.
STOCK = """
tools:
  vgc10: {code: 1, count: 1}
  2fg7: {code: 2, count: 1}
  rq140: {code: 4}
actions:
  coger: {mission: coger, tool_input: garra}
robots:
  sim-1: {driver: sim, tool: "", tool_changer: [vgc10, 2fg7], actions: [coger]}
  sim-2: {driver: sim, tool: "", tool_changer: [vgc10, 2fg7], actions: [coger]}
  sim-3: {driver: sim, tool: rq140, actions: [coger]}
"""


def test_config_count(tmp_path):
    cfg = _cfg(tmp_path, STOCK)
    assert cfg.tools["vgc10"] == {"code": 1, "description": "", "count": 1} and "count" not in cfg.tools["rq140"]
    for bad in ("0", "true", "1.5"):
        with pytest.raises(ConfigError, match="count debe ser un entero"):
            _cfg(tmp_path, STOCK.replace("vgc10: {code: 1, count: 1}", f"vgc10: {{code: 1, count: {bad}}}"))


def test_tool_holders_y_stock_problem():
    robots = [("a", {"v"}, "v", None),          # la lleva
              ("b", {"v"}, "", "v"),            # va a buscarla
              ("c", {"v"}, None, None),         # no se sabe qué lleva: por prudencia, ocupa
              ("d", {"x"}, "v", None),          # no es del soporte de `v` (no la tiene en su cambiador)
              ("e", {"v"}, "", None)]           # libre, sin ella
    assert tool_holders("v", robots) == ["a", "b", "c"]
    assert stock_problem("v", None, ["a"], "e", "") == ""                      # sin count: no se cuenta
    assert stock_problem("v", 1, ["a"], "a", "v") == ""                        # la lleva él
    assert stock_problem("v", 1, ["b"], "b", "") == ""                         # la pidió él
    assert stock_problem("v", 4, ["a", "b", "c"], "e", "") == ""               # queda una
    assert stock_problem("v", 3, ["a", "b", "c"], "e", "") == "no queda v libre en el soporte (ocupada por a, b, c)"


def test_la_segunda_order_espera_a_que_quede_la_ventosa(tmp_path):
    disp, robots, bus = _fleet(tmp_path, STOCK)
    _send(disp, "o1", "vgc10")
    assert _resp(bus, "o1")["assignedSerial"] == "sim-1"            # alfabético; va a por ella
    _send(disp, "o2", "vgc10")                                       # sim-2 está libre, pero no hay otra
    r = _resp(bus, "o2")
    assert r["status"] == "QUEUED"
    assert "sim-2: no queda vgc10 libre en el soporte (ocupada por sim-1)" in r["description"]
    assert "sim-3: lleva rq140, no vgc10 (sin cambiador)" in r["description"]
    # Otra herramienta sí: sim-2 va a por la 2fg7.
    _send(disp, "o3", "2fg7")
    assert _resp(bus, "o3")["assignedSerial"] == "sim-2"
    # sim-1 termina y se queda con la ventosa puesta: la order de la cola es para él.
    robots["sim-1"].driver.clock = lambda: 10**6
    for r in robots.values():
        r.poll()
    disp.tick()
    assert _resp(bus, "o2")["assignedSerial"] == "sim-1"


def test_varias_herramientas_con_el_soporte(tmp_path):
    """Decisión 211: con la ventosa ya en sim-1, una order que vale con la
    ventosa o la pinza va a sim-2 con la pinza (la mission recibe una sola)."""
    disp, robots, bus = _fleet(tmp_path, STOCK)
    _send(disp, "o1", "vgc10")
    assert _resp(bus, "o1")["assignedSerial"] == "sim-1"
    _send(disp, "o2", "vgc10,2fg7")
    assert _resp(bus, "o2")["assignedSerial"] == "sim-2"
    assert robots["sim-2"].requested_tool() == "2fg7"
    # Las dos ocupadas y sim-3 sin ninguna de ellas: la tercera espera y la cola dice lo que pide.
    _send(disp, "o3", "2fg7,vgc10")
    assert _resp(bus, "o3")["status"] == "QUEUED"
    queue = [p for t, p in bus.published if t.endswith("/queue")][-1]["tasks"]
    assert queue[0]["orderId"] == "o3" and queue[0]["tool"] == "2fg7,vgc10"


def test_order_dirigida_con_varias_herramientas(tmp_path):
    disp, robots, bus = _fleet(tmp_path, STOCK)
    _send(disp, "o1", "vgc10")                                       # sim-1 va a por la ventosa
    disp.handle(topic("SIM", "sim-2", "order"), _order("d1", "vgc10,2fg7", serial="sim-2", man="SIM"))
    assert robots["sim-2"].requested_tool() == "2fg7"                # la ventosa no queda: la pinza
    disp.handle(topic("SIM", "sim-3", "order"), _order("d2", "vgc10,2fg7", serial="sim-3", man="SIM"))
    errors = [e for e in robots["sim-3"].orders.order_errors if e.errorType == "TOOL_NOT_AVAILABLE"]
    assert errors and "lleva rq140, no vgc10 ni 2fg7 (sin cambiador)" in errors[0].errorDescription


def test_tipo_de_tarea_con_sus_herramientas(tmp_path):
    """La order no dice herramienta: el FM usa las del tipo de tarea, en su orden."""
    yaml = STOCK.replace("coger: {mission: coger, tool_input: garra}",
                         "coger: {mission: coger, tool_input: garra, tools: [2fg7, vgc10]}")
    disp, robots, bus = _fleet(tmp_path, yaml)
    _send(disp, "o1")                                                # sin parámetro: la primera, 2fg7
    assert _resp(bus, "o1")["assignedSerial"] == "sim-1" and robots["sim-1"].requested_tool() == "2fg7"
    _send(disp, "o2")                                                # 2fg7 ya no queda: vgc10
    assert _resp(bus, "o2")["assignedSerial"] == "sim-2" and robots["sim-2"].requested_tool() == "vgc10"
    _send(disp, "o3", "rq140")                                       # fuera de las del tipo de tarea
    r = _resp(bus, "o3")
    assert r["status"] == "REJECTED" and "no vale para 'coger'" in r["errorDescription"]
    # sim-3 (rq140 fija) no puede con ninguna de las del tipo de tarea; dirigida, se rechaza con el motivo.
    disp.handle(topic("SIM", "sim-3", "order"), _order("d1", serial="sim-3", man="SIM"))
    errors = [e for e in robots["sim-3"].orders.order_errors if e.errorType == "TOOL_NOT_AVAILABLE"]
    assert errors and "lleva rq140, no 2fg7 ni vgc10 (sin cambiador)" in errors[0].errorDescription
    # Para la web: no obligatoria y en el orden del tipo de tarea.
    p = robots["sim-1"].driver.describe_actions()[0].params[0]
    assert (p.key, p.required, p.choices) == ("garra", False, ["2fg7", "vgc10"])


def test_sin_count_no_se_cuenta(tmp_path):
    disp, robots, bus = _fleet(tmp_path, STOCK.replace("vgc10: {code: 1, count: 1}", "vgc10: {code: 1}"))
    _send(disp, "o1", "vgc10")
    _send(disp, "o2", "vgc10")
    assert {_resp(bus, "o1")["assignedSerial"], _resp(bus, "o2")["assignedSerial"]} == {"sim-1", "sim-2"}


def test_robot_sin_respuesta_sigue_ocupando_la_que_llevaba(tmp_path):
    disp, robots, bus = _fleet(tmp_path, STOCK.replace('sim-1: {driver: sim, tool: ""', 'sim-1: {driver: sim, tool: vgc10'))
    assert robots["sim-1"].known_tool == "vgc10"
    robots["sim-1"].last_telemetry = None                            # se cae la red: no se sabe ahora…
    _send(disp, "o1", "vgc10")                                       # …pero la última vez la llevaba
    r = _resp(bus, "o1")
    assert r["status"] == "QUEUED" and "no queda vgc10 libre en el soporte (ocupada por sim-1)" in r["description"]


def test_order_dirigida_sin_unidad_libre(tmp_path):
    disp, robots, bus = _fleet(tmp_path, STOCK)
    _send(disp, "o1", "vgc10")                                       # sim-1 va a por ella
    disp.handle(topic("SIM", "sim-2", "order"), _order("d1", "vgc10", serial="sim-2", man="SIM"))
    errors = [e for e in robots["sim-2"].orders.order_errors if e.errorType == "TOOL_NOT_AVAILABLE"]
    assert errors and "ocupada por sim-1" in errors[0].errorDescription
    assert robots["sim-2"].driver._jobs == {}
    disp.handle(topic("SIM", "sim-2", "order"), _order("d2", "2fg7", serial="sim-2", man="SIM"))
    assert robots["sim-2"].driver._jobs                              # la otra sí está en el soporte
