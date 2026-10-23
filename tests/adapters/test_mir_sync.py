"""Sincronización del catálogo con un MiR (fm/adapters/mir/sync.py), sin red.

`FakeMir` imita lo que se vio con robot el 2026-10-13: acepta GUIDs elegidos,
da 409 si se repiten, crea solo la entrada de un marcador/cargador (con GUID
propio y en una posición por defecto) y pone GUIDs propios a las actions y a
sus parámetros.
"""
import base64
import json
import uuid

import pytest

from fm.adapters.mir.client import MirApiError
from fm.adapters.mir.sync import Catalog, RobotView, apply, fetch_view, plan, std_guid

SES = std_guid("session", 1, 0, 1)
MAP = std_guid("map", 1, 1, 1)
P_NORMAL = std_guid("position", 1, 1, 1)
P_MARKER = std_guid("position", 1, 1, 2)
P_ENTRY = std_guid("position", 1, 1, 3)
P_OTHER = std_guid("position", 1, 1, 4)
GRP = std_guid("missiongrp", 1, 0, 1)
SND = std_guid("sound", 1, 0, 1)
M_SUB = std_guid("mission", 1, 1, 1)
M_MAIN = std_guid("mission", 1, 1, 2)
PNG = b"\x89PNG\r\n\x1a\nfalso"


def move(prio, pos, scope=None):
    return {"priority": prio, "action_type": "move", "scope": scope,
            "parameters": [{"id": "position", "value": pos, "input_name": None}]}


def write_catalog(root):
    def put(folder, doc):
        (root / folder).mkdir(parents=True, exist_ok=True)
        (root / folder / f"{doc['guid']}.json").write_text(json.dumps(doc))
    put("sessions", {"guid": SES, "name": "s"})
    put("maps", {"guid": MAP, "name": "m", "session_id": SES, "resolution": 0.05, "origin_x": -1.0,
                 "origin_y": -2.0, "origin_theta": 0.0, "base_map": f"{MAP}.png"})
    (root / "maps" / f"{MAP}.png").write_bytes(PNG)
    pos = {"map_id": MAP, "parent_id": None, "docking_offsets": [], "creada_por_mir": False,
           "_origen": {"robot": "mir-2"}}
    put("positions", {**pos, "guid": P_NORMAL, "name": "A", "type_id": 0, "pos_x": 1.0, "pos_y": 2.0, "orientation": 90.0})
    put("positions", {**pos, "guid": P_MARKER, "name": "VL", "type_id": 11, "pos_x": 5.0, "pos_y": 5.0,
                      "orientation": 180.0, "docking_offsets": [{"x_offset": -1.0, "y_offset": 0.1, "orientation_offset": 2.0}]})
    put("positions", {**pos, "guid": P_ENTRY, "name": "VL", "type_id": 12, "pos_x": 6.0, "pos_y": 5.0,
                      "orientation": -179.0, "parent_id": P_MARKER, "creada_por_mir": True})
    put("positions", {**pos, "guid": P_OTHER, "name": "B", "type_id": 0, "pos_x": 3.0, "pos_y": 3.0, "orientation": 0.0})
    put("sounds", {"guid": SND, "name": "pitido", "note": "", "volume": 80, "sound": f"{SND}.ogg"})
    (root / "sounds" / f"{SND}.ogg").write_bytes(b"OggS...")
    put("mission_groups", {"guid": GRP, "name": "g"})
    mission = {"description": "", "group_id": GRP, "session_id": SES, "hidden": False}
    put("missions", {**mission, "guid": M_SUB, "name": "sub", "actions": [
        move(1, P_NORMAL),
        {"priority": 2, "action_type": "sound", "scope": None,
         "parameters": [{"id": "sound", "value": SND, "input_name": None}]}]})
    put("missions", {**mission, "guid": M_MAIN, "name": "principal", "actions": [
        {"priority": 1, "action_type": "try_catch", "scope": None,
         "parameters": [{"id": "try", "value": "", "input_name": None},
                        {"id": "catch", "value": "", "input_name": None}]},
        move(2, P_MARKER, {"action": 1, "parameter": "try"}),
        move(3, P_NORMAL, {"action": 1, "parameter": "catch"}),
        {"priority": 4, "action_type": "load_mission", "scope": None,
         "parameters": [{"id": "mission_id", "value": M_SUB, "input_name": None}]}]})
    return Catalog.load(root)


class FakeMir:
    """REST mínimo del MiR en memoria: `_req(method, path, json)` como MirClient."""

    def __init__(self):
        self.db = {"sessions": {}, "maps": {}, "positions": {}, "sounds": {}, "mission_groups": {},
                   "missions": {}, "actions": {}}
        self.writes: list[tuple[str, str]] = []
        self.status = {"map_id": MAP, "position": {"x": 1.23456, "y": 4.5, "orientation": 89.987}}

    def _req(self, method, path, json=None):
        parts = path.split("/")
        if method != "GET":
            self.writes.append((method, path))
        if method == "POST" and len(parts) == 1:
            table = self.db[parts[0]]
            g = json.get("guid") or str(uuid.uuid4())
            if g in table:
                raise MirApiError(method, path, 409, {"error_code": "duplicate_entry"})
            doc = {**json, "guid": g}
            table[g] = doc
            if parts[0] == "positions":
                doc.setdefault("parent_id", None)
                if doc["type_id"] in (11, 20):    # el MiR crea la entrada solo, 1 m más allá
                    e = str(uuid.uuid4())
                    self.db["positions"][e] = {"guid": e, "name": doc["name"], "type_id": doc["type_id"] + 1,
                                               "pos_x": doc["pos_x"] + 9.0, "pos_y": doc["pos_y"],
                                               "orientation": 0.0, "map_id": doc["map_id"], "parent_id": g}
            return doc
        if method == "POST" and parts[0] == "missions" and parts[2] == "actions":
            a = {**json, "guid": str(uuid.uuid4()), "mission_id": parts[1],
                 "scope_reference": json.get("scope_reference"),
                 "parameters": [{**p, "guid": str(uuid.uuid4())} for p in json["parameters"]]}
            self.db["actions"][a["guid"]] = a
            return a
        if method == "DELETE" and len(parts) == 2:
            if parts[1] not in self.db[parts[0]]:
                raise MirApiError(method, path, 404, {})
            del self.db[parts[0]][parts[1]]
            if parts[0] == "missions":                     # y las actions con su mission (visto en mir-2)
                for g in [g for g, a in self.db["actions"].items() if a["mission_id"] == parts[1]]:
                    del self.db["actions"][g]
            if parts[0] == "positions":                    # el MiR borra la entrada con su marcador
                for g in [g for g, p in self.db["positions"].items() if p.get("parent_id") == parts[1]]:
                    del self.db["positions"][g]
            return None
        if method == "PUT" and parts[0] == "positions":
            self.db["positions"][parts[1]].update(json)
            return self.db["positions"][parts[1]]
        if method == "GET":
            if path == "status":
                return self.status
            if parts[0] == "mission_groups" and len(parts) == 3:
                return [m for m in self.db["missions"].values() if m["group_id"] == parts[1]]
            if parts[0] == "positions" and len(parts) == 3 and parts[2] == "docking_offsets":
                return []
            if len(parts) == 1:
                return list(self.db[parts[0]].values())
            if parts[0] == "maps" and len(parts) == 3:
                return [p for p in self.db["positions"].values() if p["map_id"] == parts[1]]
            if parts[0] == "positions" and len(parts) == 3:
                return [p for p in self.db["positions"].values() if p["parent_id"] == parts[1]]
            if parts[0] == "missions" and len(parts) == 3:
                return [a for a in self.db["actions"].values() if a["mission_id"] == parts[1]]
            if parts[0] == "missions" and len(parts) == 4:
                return self.db["actions"][parts[3]]
            if parts[1] in self.db[parts[0]]:
                return self.db[parts[0]][parts[1]]
            raise MirApiError(method, path, 404, {})
        raise AssertionError(f"llamada no simulada: {method} {path}")


@pytest.fixture
def cat(tmp_path):
    return write_catalog(tmp_path)


def test_una_mission_arrastra_sus_dependencias_en_orden(cat):
    order = cat.closure([M_MAIN])
    assert order == [SES, MAP, P_NORMAL, P_MARKER, P_ENTRY, SND, GRP, M_SUB, M_MAIN]
    assert P_OTHER not in order           # no se usa: no viaja


def test_un_marcador_arrastra_su_entrada_y_su_mapa(cat):
    assert cat.closure([P_MARKER]) == [SES, MAP, P_MARKER, P_ENTRY]


def test_guid_desconocido(cat):
    with pytest.raises(ValueError):
        cat.closure(["imperialfleet-mission000-01-0001-999"])


def test_plan_robot_vacio_todo_por_crear_y_aviso_de_docking(cat):
    guids = cat.closure([M_MAIN])
    ops = plan(cat, RobotView(), guids, "mir-1")
    assert [o.verb for o in ops] == ["crear"] * len(guids)
    marker = next(o for o in ops if o.guid == P_MARKER)
    assert "mir-2" in marker.warning
    assert not plan(cat, RobotView(), guids, "mir-2")[3].warning   # mismo robot: sin aviso


def test_desplegar_y_repetir_no_cambia_nada(cat):
    mir = FakeMir()
    guids = cat.closure([M_MAIN])
    ops = apply(mir, cat, plan(cat, fetch_view(mir, cat, guids), guids, "mir-1"))
    assert {o.status for o in ops} == {"hecho"}, [(o.name, o.status, o.detail) for o in ops]
    # La entrada creada por el MiR se ha movido a donde dice el catálogo.
    entry = next(p for p in mir.db["positions"].values() if p["parent_id"] == P_MARKER)
    assert (entry["pos_x"], entry["orientation"]) == (6.0, -179.0)
    # El try/catch apunta a los GUIDs de parámetro que puso el MiR.
    acts = {a["priority"]: a for a in mir.db["actions"].values() if a["mission_id"] == M_MAIN}
    try_guid = next(p["guid"] for p in acts[1]["parameters"] if p["id"] == "try")
    assert acts[2]["scope_reference"] == try_guid
    # El mapa sube con su imagen.
    assert base64.b64decode(mir.db["maps"][MAP]["base_map"]) == PNG

    n = len(mir.writes)
    ops = plan(cat, fetch_view(mir, cat, guids), guids, "mir-1")
    assert {o.verb for o in ops} == {"igual"}, [(o.name, o.verb, o.detail) for o in ops]
    apply(mir, cat, ops)
    assert len(mir.writes) == n             # idempotente: ni una escritura


def test_distinta_no_se_toca_y_entrada_movida_se_corrige(cat):
    mir = FakeMir()
    guids = cat.closure([P_MARKER, P_NORMAL])
    apply(mir, cat, plan(cat, fetch_view(mir, cat, guids), guids, "mir-2"))
    mir.db["positions"][P_NORMAL]["pos_x"] = 99.0                 # alguien la mueve a mano
    entry = next(p for p in mir.db["positions"].values() if p["parent_id"] == P_MARKER)
    entry["pos_x"] = 7.5
    ops = {o.guid: o for o in plan(cat, fetch_view(mir, cat, guids), guids, "mir-2")}
    assert ops[P_NORMAL].verb == "distinta" and "pose" in ops[P_NORMAL].detail
    assert ops[P_ENTRY].verb == "corregir" and ops[P_ENTRY].target == entry["guid"]
    apply(mir, cat, list(ops.values()))
    assert mir.db["positions"][P_NORMAL]["pos_x"] == 99.0          # no se toca
    assert entry["pos_x"] == 6.0                                   # se corrige


def test_si_falla_una_dependencia_se_saltan_las_que_la_usan(cat):
    mir = FakeMir()
    real = mir._req

    def failing(method, path, json=None):
        if method == "POST" and path == "maps":
            raise MirApiError(method, path, 400, {"error_code": "invalid_input_data"})
        return real(method, path, json)
    mir._req = failing
    guids = cat.closure([P_NORMAL])
    ops = apply(mir, cat, plan(cat, fetch_view(mir, cat, guids), guids, "mir-2"))
    status = {o.guid: o.status for o in ops}
    assert status == {SES: "hecho", MAP: "error", P_NORMAL: "saltada"}


def test_required_y_comprobaciones_rapida_y_completa(cat):
    from fm.adapters.mir.sync import MirSync
    s = MirSync("mir-1", "0.0.0.0", "Basic x", cat.root, wanted=["principal", M_SUB, "no-catalogada"])
    s.client = FakeMir()
    required, outside = s.required(cat)
    assert required == cat.closure([M_MAIN]) and outside == ["no-catalogada"]   # por nombre o por GUID

    r = s.check()
    assert r["level"] == "rápida" and set(r["state"].values()) == {"falta"}
    s.deploy([M_MAIN])
    r = s.check()
    assert {g: v for g, v in r["state"].items() if v != "ok"} == {P_OTHER: "falta"}   # no se usa
    s.client.db["missions"][M_SUB]["name"] = "editada en la web"
    assert s.check()["state"][M_SUB] == "ok"              # la rápida solo mira si existe
    assert s.check(deep=True)["state"][M_SUB] == "distinta"


def test_action_needs_para_la_matriz_del_configurador(cat):
    from fm.adapters.mir.sync import MirSync
    s = MirSync("mir-1", "0.0.0.0", "Basic x", cat.root)
    needs = s.action_needs({"por_guid": {"mission": M_MAIN}, "por_nombre": {"mission": "sub"},
                            "fuera": {"mission": "no-catalogada"}, "sin_mission": {}})
    assert needs["por_guid"]["required"] == cat.closure([M_MAIN])
    assert needs["por_guid"]["mission_name"] == "principal"
    assert needs["por_nombre"]["mission_guid"] == M_SUB
    assert needs["fuera"] == {"mission": "no-catalogada", "mission_guid": None,
                              "mission_name": None, "required": []}
    assert needs["sin_mission"]["mission_guid"] is None


def test_form_choices_inputs_y_su_tipo(cat):
    from fm.adapters.mir.sync import MirSync, guess_input_kind
    assert guess_input_kind({"id": "position", "value": "x"}) == "position"
    assert guess_input_kind({"id": "value", "value": 1.0}) == "number"
    assert guess_input_kind({"id": "value", "value": "hola"}) == "text"
    ch = MirSync("mir-1", "0.0.0.0", "Basic x", cat.root).form_choices()
    assert {m["guid"] for m in ch["missions"]} == {M_MAIN, M_SUB}
    assert all(isinstance(m["inputs"], list) for m in ch["missions"])
    assert isinstance(ch["positions"], list)


def test_deploy_solo_crear_no_corrige_nada(cat):
    # Robot ocupado (decisión 103): crear sí; corregir lo que ya tiene, no.
    from fm.adapters.mir.sync import MirSync
    from tests.adapters.test_mir_sync import P_MARKER
    s = MirSync("mir-2", "0.0.0.0", "Basic x", cat.root)
    s.client = FakeMir()
    ops = s.deploy([P_MARKER], only_create=True)                  # robot vacío: todo es crear
    assert {o.status for o in ops} == {"hecho"}
    entry = next(p for p in s.client.db["positions"].values() if p["parent_id"] == P_MARKER)
    entry["pos_x"] += 3.0                                           # alguien la mueve en la web
    before = list(s.client.writes)
    with pytest.raises(RuntimeError, match="ocupado"):
        s.deploy([P_MARKER], only_create=True)
    assert s.client.writes == before                                # no se ha tocado nada
    assert any(o.status == "hecho" for o in s.deploy([P_MARKER]))   # libre: se corrige


def test_sustituir_mission_distinta_solo_si_se_pide(cat):
    # Decisión 117: una mission distinta no se toca salvo que se marque.
    from fm.adapters.mir.sync import MirSync, normalized_actions
    mir = FakeMir()
    guids = cat.closure([M_MAIN])
    apply(mir, cat, plan(cat, fetch_view(mir, cat, guids), guids, "mir-1"))
    # En la web del MiR alguien le añade una action a «sub» (que «principal» carga)
    mir._req("POST", f"missions/{M_SUB}/actions", json={"action_type": "wait", "priority": 3,
                                                       "parameters": [{"id": "time", "value": "00:00:01", "input_name": None}]})
    ops = {o.guid: o for o in plan(cat, fetch_view(mir, cat, guids), guids, "mir-1")}
    assert ops[M_SUB].verb == "distinta" and "márcala" in ops[M_SUB].detail
    n = len(mir.writes)
    apply(mir, cat, list(ops.values()))
    assert len(mir.writes) == n                                     # sin marcar: nada
    ops = {o.guid: o for o in plan(cat, fetch_view(mir, cat, guids), guids, "mir-1", replace=[M_SUB])}
    assert ops[M_SUB].verb == "sustituir" and "principal" in ops[M_SUB].warning   # la carga otra: aviso
    assert ops[M_MAIN].verb == "igual"
    apply(mir, cat, list(ops.values()))
    assert ops[M_SUB].status == "hecho"
    assert ("DELETE", f"missions/{M_SUB}") in mir.writes
    assert normalized_actions(mir, M_SUB) == cat.items[M_SUB]["actions"]   # ya como el catálogo
    # Con el robot ocupado no se sustituye nada (como corregir, decisión 103)
    s = MirSync("mir-1", "0.0.0.0", "Basic x", cat.root)
    s.client = mir
    mir._req("POST", f"missions/{M_SUB}/actions", json={"action_type": "wait", "priority": 3,
                                                       "parameters": [{"id": "time", "value": "00:00:01", "input_name": None}]})
    before = list(mir.writes)
    with pytest.raises(RuntimeError, match="ocupado"):
        s.deploy([M_MAIN], only_create=True, replace=[M_SUB])
    assert mir.writes == before
