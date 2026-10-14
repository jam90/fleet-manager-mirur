"""Pestaña Crear (fm/adapters/mir/create.py), sin red: alta de una mission
vacía y de una position con GUID estándar, borrador en el catálogo y captura."""
import json
import threading

import pytest

from fm.adapters.mir.create import CreateError, next_guid
from fm.adapters.mir.sync import Catalog, MirSync, std_guid
from tests.adapters.test_mir_sync import GRP, M_MAIN, M_SUB, MAP, P_NORMAL, SES, FakeMir, write_catalog


@pytest.fixture
def sync(tmp_path):
    write_catalog(tmp_path)
    s = MirSync("mir-2", "10.0.0.2", "Basic x", tmp_path)
    s.client = FakeMir()
    s.client.host = "10.0.0.2"
    # El robot ya tiene el grupo y el mapa del catálogo (desplegados antes).
    s.client.db["mission_groups"][GRP] = {"guid": GRP, "name": "g"}
    s.client.db["maps"][MAP] = {"guid": MAP, "name": "m"}
    return s


def test_next_guid_por_instalacion_y_por_mapa():
    taken = [std_guid("mission", 1, 1, 7), std_guid("mission", 1, 2, 3), "c7a22ec6-371d-11f1-99c4-000e8eb69199"]
    assert next_guid("mission", 1, 1, taken, per_parent=False) == std_guid("mission", 1, 1, 8)
    pos = [std_guid("position", 1, 1, 4), std_guid("position", 1, 2, 9)]
    assert next_guid("position", 1, 1, pos, per_parent=True) == std_guid("position", 1, 1, 5)


def test_crear_mission_vacia_con_guid_y_borrador(sync, tmp_path):
    # Otro robot ya tiene una mission estándar con número más alto: se salta.
    otro = std_guid("mission", 1, 1, 7)
    r = sync.create_mission("nueva", GRP, others={otro})
    assert r["guid"] == std_guid("mission", 1, 1, 8)
    assert r["url"] == f"http://10.0.0.2/missions/{r['guid']}"
    robot = sync.client.db["missions"][r["guid"]]
    assert robot["group_id"] == GRP and robot["session_id"] == SES and robot["name"] == "nueva"
    cat = Catalog.load(tmp_path)
    assert r["guid"] in cat.drafts and r["guid"] not in cat.items      # no se despliega
    assert cat.drafts[r["guid"]]["actions"] == [] and cat.drafts[r["guid"]]["_origen"]["robot"] == "mir-2"
    # el número queda reservado por el borrador aunque el robot ya no la tenga
    del sync.client.db["missions"][r["guid"]]
    assert sync.create_mission("otra", GRP)["guid"] == std_guid("mission", 1, 1, 9)


def test_crear_mission_errores(sync):
    with pytest.raises(CreateError, match="falta el nombre"):
        sync.create_mission("  ", GRP)
    with pytest.raises(CreateError, match="ya hay una mission 'principal'"):
        sync.create_mission("principal", GRP)                 # está en el catálogo
    del sync.client.db["mission_groups"][GRP]
    with pytest.raises(CreateError, match="despliégalo antes"):
        sync.create_mission("x", GRP)


def test_crear_position_donde_esta_el_robot(sync, tmp_path):
    r = sync.create_position("Mesa nueva", MAP)
    assert r["guid"] == std_guid("position", 1, 1, 5)             # el catálogo tiene 1..4 en ese mapa
    assert r["url"] == f"http://10.0.0.2/maps/{MAP}"
    p = sync.client.db["positions"][r["guid"]]
    assert (p["type_id"], p["pos_x"], p["pos_y"], p["orientation"]) == (0, 1.235, 4.5, 89.99)
    assert r["guid"] in Catalog.load(tmp_path).drafts
    with pytest.raises(CreateError, match="ya hay una position 'Mesa nueva'"):
        sync.create_position("Mesa nueva", MAP)
    sync.client.status["map_id"] = "otro-mapa"
    with pytest.raises(CreateError, match="no está trabajando en el mapa"):
        sync.create_position("Otra", MAP)


def test_capturar_mission_creada_y_editada_en_el_mir(sync, tmp_path):
    r = sync.create_mission("nueva", GRP)
    g = r["guid"]
    # En la web del MiR se le añade un move a una position del catálogo.
    sync.client._req("POST", f"missions/{g}/actions", json={
        "action_type": "move", "priority": 1, "parameters": [{"id": "position", "value": P_NORMAL,
                                                             "input_name": "destino"}]})
    out = sync.capture(g)
    assert out["detail"] == "1 actions"
    doc = Catalog.load(tmp_path).items[g]
    assert "borrador" not in doc and doc["actions"][0]["parameters"][0]["value"] == P_NORMAL
    assert doc["session_id"] == SES and doc["group_id"] == GRP


def test_capturar_rechaza_referencias_fuera_del_catalogo(sync, tmp_path):
    g = sync.create_mission("nueva", GRP)["guid"]
    sync.client._req("POST", f"missions/{g}/actions", json={
        "action_type": "move", "priority": 1,
        "parameters": [{"id": "position", "value": "c7a22ec6-371d-11f1-99c4-000e8eb69199", "input_name": None}]})
    with pytest.raises(CreateError, match="no está en el catálogo"):
        sync.capture(g)
    assert g in Catalog.load(tmp_path).drafts                          # sigue de borrador


def test_capturar_position_movida_en_el_mapa(sync, tmp_path):
    g = sync.create_position("Mesa", MAP)["guid"]
    sync.client.db["positions"][g].update(pos_x=7.0, name="Mesa 2")
    sync.capture(g)
    doc = Catalog.load(tmp_path).items[g]
    assert (doc["name"], doc["pos_x"], doc["type_id"]) == ("Mesa 2", 7.0, 0) and doc["docking_offsets"] == []


def test_capturar_solo_desde_el_robot_de_origen(sync):
    g = sync.create_mission("nueva", GRP)["guid"]
    otro = MirSync("mir-1", "10.0.0.1", "Basic x", sync.catalog_dir)
    with pytest.raises(CreateError, match="se creó en mir-2"):
        otro.capture(g)


def test_choices_lista_grupos_mapas_y_lo_creado(sync):
    g1 = sync.create_mission("nueva", GRP)["guid"]
    sync.create_mission("otra", GRP)
    sync.capture(g1)
    ch = sync.create_choices()
    assert [x["guid"] for x in ch["groups"]] == [GRP] and [x["guid"] for x in ch["maps"]] == [MAP]
    # borradores primero; lo capturado del catálogo con GUID de otro robot (script) no sale
    assert [(d["name"], d["draft"]) for d in ch["created"]] == [("otra", True), ("nueva", False)]


# ------------------------------------------------------------------- borrar
def test_borrar_lo_creado_libera_el_numero(sync, tmp_path):
    g = sync.create_mission("prueba", GRP)["guid"]
    assert sync.delete_blockers(g, {}, []) == []
    assert sync.exists(g)
    sync.delete(g)
    assert not sync.exists(g) and sync.forget(g) == [g]
    cat = Catalog.load(tmp_path)
    assert g not in cat.drafts and g not in cat.items
    assert sync.create_mission("otra", GRP)["guid"] == g          # era el más alto: se reutiliza


def test_borrar_bloqueos(sync):
    assert "pestaña Crear" in sync.delete_blockers(M_MAIN, {}, [])[0]     # capturada con el script
    g = sync.create_mission("prueba", GRP)["guid"]
    assert "fleet.yaml (actions: probar)" in sync.delete_blockers(g, {"probar": {"mission": g}}, [])[0]
    assert "fleet.yaml (actions: probar)" in sync.delete_blockers(g, {"probar": {"mission": "prueba"}}, [])[0]
    assert "fleet.yaml" in sync.delete_blockers(g, {}, [g])[0]           # p.ej. mission de carga
    p = sync.create_position("Mesa", MAP)["guid"]
    sync.capture(p)
    sync.client._req("POST", f"missions/{g}/actions", json={
        "action_type": "move", "priority": 1, "parameters": [{"id": "position", "value": p, "input_name": None}]})
    sync.capture(g)
    assert "la usan otros recursos del catálogo: prueba" in sync.delete_blockers(p, {}, [])[0]


def test_borrar_marcador_se_lleva_su_entrada(sync, tmp_path):
    p = sync.create_position("VL nueva", MAP)["guid"]
    sync.client.db["positions"][p]["type_id"] = 11                       # convertida en marcador en la web
    sync.client._req("POST", "positions", json={"name": "VL nueva", "type_id": 12, "pos_x": 0, "pos_y": 0,
                                                "orientation": 0, "map_id": MAP, "parent_id": p})
    sync.client._req = (lambda real: (lambda m, path, json=None: [x for x in sync.client.db["positions"].values()
                        if x.get("parent_id") == p] if path.endswith("/helper_positions") else real(m, path, json)))(sync.client._req)
    sync.capture(p)
    entry = next(g for g, d in Catalog.load(tmp_path).items.items() if d.get("parent_id") == p)
    assert sync.delete_blockers(p, {}, []) == []
    sync.delete(p)
    assert sorted(sync.forget(p)) == sorted([p, entry])
