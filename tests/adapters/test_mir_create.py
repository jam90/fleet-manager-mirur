"""Pestaña Crear (fm/adapters/mir/create.py), sin red: alta de una mission
vacía y de una position con GUID estándar, borrador en el catálogo y captura."""
import json
import threading

import pytest

from fm.adapters.mir.create import CreateError, next_guid
from fm.adapters.mir.sync import Catalog, MirSync, parse_std, std_guid
from tests.adapters.test_mir_sync import GRP, M_MAIN, M_SUB, MAP, P_NORMAL, SES, SND, FakeMir, write_catalog, move


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


def _web_mission(sync, name, *actions, group=GRP):
    """Una mission hecha en la web del MiR: GUID del robot, no de la flota."""
    g = "181aad0a-ab8f-7cfc-bd76-96cc29f883dc"
    sync.client.db["missions"][g] = {"guid": g, "name": name, "group_id": group, "session_id": "s-robot",
                                     "description": "", "hidden": False}
    for a in actions:
        sync.client._req("POST", f"missions/{g}/actions", json={k: v for k, v in a.items() if k != "scope"})
    return g


def test_adoptar_en_lugar_de_una_del_catalogo(sync, tmp_path):
    # Decisión 116: «sub» se borró en la web y se rehízo con «Guardar como»
    src = _web_mission(sync, "sub", move(1, P_NORMAL), move(2, P_NORMAL))
    sync.client.writes.clear()                                            # lo de arriba es «la web»
    rows = sync.adoptable()
    assert [(r["guid"], r["suggest"], r["adopted_as"]) for r in rows] == [(src, M_SUB, None)]
    out = sync.adopt(src, M_SUB)
    assert out["guid"] == M_SUB and out["replaced"] and out["detail"] == "2 actions"
    doc = Catalog.load(tmp_path).items[M_SUB]
    assert len(doc["actions"]) == 2 and doc["session_id"] == SES          # la session, la del catálogo
    assert doc["_origen"]["robot"] == "mir-2" and doc["_origen"]["guid"] == src
    assert sync.client.writes == []                                       # el robot no se toca
    # Hasta que se borre la copia en la web, sale como ya adoptada
    assert sync.adoptable()[0]["adopted_as"] == M_SUB


def test_adoptar_como_nueva_y_errores(sync, tmp_path):
    src = _web_mission(sync, "otra", move(1, P_NORMAL))
    out = sync.adopt(src)
    assert out["guid"] not in (M_SUB, M_MAIN) and not out["replaced"]
    assert Catalog.load(tmp_path).items[out["guid"]]["name"] == "otra"
    with pytest.raises(CreateError, match="Capturar"):
        sync.adopt(M_SUB)                                                 # ya tiene GUID de la flota
    with pytest.raises(CreateError, match="no es una mission"):
        sync.adopt(src, GRP)
    bad = _web_mission(sync, "rota", {"priority": 1, "action_type": "move", "scope": None, "parameters": [
        {"id": "position", "value": "c7a22ec6-371d-11f1-99c4-000e8eb69199", "input_name": None}]})
    with pytest.raises(CreateError, match="no está en el catálogo"):
        sync.adopt(bad, M_SUB)


def test_crear_vl_marker_y_cargador(sync, tmp_path):
    # Decisión 119: el tipo se elige al crear (la web no deja cambiarlo).
    r = sync.create_position("Mesa nueva", MAP, type_id=11)
    assert r["type"] == "VL-marker" and r["marker"]
    sent = sync.client.db["positions"][r["guid"]]
    assert sent["type_id"] == 11 and sent["offset_x"] == -0.8                  # el MiR exige offset
    entry = [p for p in sync.client.db["positions"].values() if p.get("parent_id") == r["guid"]]
    assert len(entry) == 1 and entry[0]["type_id"] == 12                      # la entrada la pone el MiR
    draft = Catalog.load(tmp_path).drafts[r["guid"]]
    assert draft["type_id"] == 11 and draft["docking_offsets"] == []           # el offset bueno, al capturar
    c = sync.create_position("Cargador", MAP, type_id=20)
    assert "offset_x" not in sync.client.db["positions"][c["guid"]]           # un cargador no lleva offset
    with pytest.raises(CreateError, match="no admitido"):
        sync.create_position("Ascensor", MAP, type_id=25)


def test_capturar_vl_marker_creado_aqui_con_su_entrada(sync, tmp_path):
    # Tras crearlo, en la web se detecta el marcador (se mueve) y se ajusta el offset.
    r = sync.create_position("Mesa nueva", MAP, type_id=11)
    g = r["guid"]
    sync.client.db["positions"][g].update(pos_x=9.5, pos_y=2.25, orientation=-93.0)
    orig = sync.client._req

    def req(method, path, json=None):                       # el docking offset que guarda el MiR
        if path == f"positions/{g}/docking_offsets":
            return [{"guid": "off-1"}]
        if path == "docking_offsets/off-1":
            return {"guid": "off-1", "x_offset": -0.75, "y_offset": 0.08, "orientation_offset": -2.7,
                    "docking_type": None, "name": None}
        return orig(method, path, json)
    sync.client._req = req
    out = sync.capture(g)
    assert out["detail"] == "tipo 11 + entrada"
    cat = Catalog.load(tmp_path)
    doc = cat.items[g]
    assert (doc["pos_x"], doc["orientation"]) == (9.5, -93.0)
    assert doc["docking_offsets"][0]["x_offset"] == -0.75 and "borrador" not in doc
    entry = [d for d in cat.items.values() if d.get("parent_id") == g]
    assert len(entry) == 1 and entry[0]["type_id"] == 12 and entry[0]["creada_por_mir"]


# -------------------------------------------------------------------- zonas
def test_crear_zona_delante_del_robot(sync, tmp_path):
    # Decisión 124: cuadrado de 1 m con su centro 2 m delante (el robot mira a +y).
    r = sync.create_zone("Pasillo", MAP, type_id=206)
    assert r["guid"] == std_guid("zone", 1, 1, 1) and r["type"] == "Zona prohibida"
    assert r["url"] == f"http://10.0.0.2/maps/{MAP}"
    z = sync.client.db["zones"][r["guid"]]
    assert z["type_id"] == 206 and z["map_id"] == MAP and z["actions"] == []
    xs, ys = [q["x"] for q in z["polygon"]], [q["y"] for q in z["polygon"]]
    assert (min(xs), max(xs), min(ys), max(ys)) == (0.735, 1.735, 6.0, 7.0)   # robot en (1.235, 4.5)
    draft = Catalog.load(tmp_path).drafts[r["guid"]]
    assert draft["polygon"] == z["polygon"] and draft["shape_type"] == "shape"
    assert sync.create_zone("Otra", MAP)["guid"] == std_guid("zone", 1, 1, 2)
    with pytest.raises(CreateError, match="ya hay una zona 'Pasillo'"):
        sync.create_zone("Pasillo", MAP)
    with pytest.raises(CreateError, match="no admitido"):
        sync.create_zone("Rara", MAP, type_id=207)            # el MiR no lo conoce
    sync.client.status["map_id"] = "otro-mapa"
    with pytest.raises(CreateError, match="no está trabajando en el mapa"):
        sync.create_zone("Lejos", MAP)


def test_capturar_zona_con_su_forma_y_sus_acciones(sync, tmp_path):
    g = sync.create_zone("Despacio", MAP, type_id=102)["guid"]
    # En la web se le da forma y se le pone un sonido del catálogo y la luz.
    sync.client._req("PUT", f"zones/{g}", json={
        "polygon": [{"x": 0, "y": 0}, {"x": 4, "y": 0}, {"x": 4, "y": 2}],
        "actions": [{"type_id": 2, "parameters": []},
                    {"type_id": 3, "parameters": [{"name": "sound", "value": SND}, {"name": "volume", "value": "40"}]}]})
    out = sync.capture(g)
    assert out["detail"] == "Zona de sonido y de luz, 3 vértices, 2 acciones"
    doc = Catalog.load(tmp_path).items[g]
    assert "borrador" not in doc and len(doc["polygon"]) == 3
    assert doc["actions"][1] == {"type_id": 3, "parameters": [{"name": "sound", "value": SND},
                                                              {"name": "volume", "value": "40"}]}  # sin GUIDs
    assert Catalog.load(tmp_path).deps(g) == {MAP, SND}


def test_capturar_zona_rechaza_un_sonido_propio_del_robot(sync, tmp_path):
    g = sync.create_zone("Pitido", MAP, type_id=102)["guid"]
    sync.client._req("PUT", f"zones/{g}", json={"actions": [
        {"type_id": 3, "parameters": [{"name": "sound", "value": "fb225e1c-3d65-11f1-8021-000e8eb5b8fb"}]}]})
    with pytest.raises(CreateError, match="no está en el catálogo"):
        sync.capture(g)
    assert g in Catalog.load(tmp_path).drafts


def test_borrar_zona_creada_aqui(sync, tmp_path):
    g = sync.create_zone("Prueba", MAP)["guid"]
    sync.capture(g)
    assert [d["kind"] for d in sync.create_choices()["created"]] == ["zone"]
    assert sync.delete_blockers(g, {}, []) == [] and sync.exists(g)
    sync.delete(g)
    assert not sync.exists(g) and sync.forget(g) == [g]
    assert sync.create_zone("Otra", MAP)["guid"] == g                # el número queda libre


# -------------------------------------------------------------------- mapas
def _png(width=4, height=3, color=0):
    """PNG mínimo válido (gris de 8 bits si `color` = 0; 2 = RGB)."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    row = b"\x00" + b"\xff" * (width * (3 if color == 2 else 1))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))


def test_subir_mapa_a_un_site_del_catalogo(sync, tmp_path):
    r = sync.create_map("mapa-nave-2", _png(40, 20), 0.05, -1.5, 2.0, session_guid=SES,
                        others={std_guid("map", 1, 1, 3)}, filename="nave2.png")
    assert r["guid"] == std_guid("map", 1, 1, 4)                  # se numera en la instalación
    assert (r["site"], r["new_site"], r["size"], r["meters"]) == ("s", False, [40, 20], [2.0, 1.0])
    cat = Catalog.load(tmp_path)
    doc = cat.items[r["guid"]]                                    # completo: no es borrador
    assert (doc["session_id"], doc["origin_x"], doc["origin_y"], doc["resolution"]) == (SES, -1.5, 2.0, 0.05)
    assert cat.map_png(r["guid"]) == _png(40, 20)
    assert cat.closure([r["guid"]]) == [SES, r["guid"]]           # Sincronización lo despliega con su site
    assert sync.client.writes == []                               # no toca el robot
    with pytest.raises(CreateError, match="ya hay un mapa 'mapa-nave-2'"):
        sync.create_map("mapa-nave-2", _png(), 0.05, 0, 0, session_guid=SES)


def test_subir_mapa_con_site_nuevo_y_errores(sync, tmp_path):
    r = sync.create_map("planta", _png(), 0.05, 0, 0, new_session="nave nueva")
    cat = Catalog.load(tmp_path)
    ses = cat.items[r["guid"]]["session_id"]
    assert ses == std_guid("session", 1, 0, 2) and cat.items[ses]["name"] == "nave nueva" and r["new_site"]
    assert r["guid"] == std_guid("map", 1, 2, 2)                  # padre = su site
    with pytest.raises(CreateError, match="gris de 8 bits"):
        sync.create_map("color", _png(color=2), 0.05, 0, 0, session_guid=SES)
    with pytest.raises(CreateError, match="no es un PNG"):
        sync.create_map("jpg", b"\xff\xd8\xff\xe0 JFIF", 0.05, 0, 0, session_guid=SES)
    with pytest.raises(CreateError, match="ya hay un site 'nave nueva'"):
        sync.create_map("otro", _png(), 0.05, 0, 0, new_session="nave nueva")
    with pytest.raises(CreateError, match="resolución"):
        sync.create_map("otro", _png(), 0, 0, 0, session_guid=SES)


def test_borrar_mapa_subido_se_lleva_su_imagen(sync, tmp_path):
    g = sync.create_map("prueba", _png(), 0.05, 0, 0, session_guid=SES)["guid"]
    assert [d["kind"] for d in sync.create_choices()["created"]] == ["map"]
    assert sync.delete_blockers(g, {}, []) == []
    assert sync.forget(g) == [g]
    assert not (tmp_path / "maps" / f"{g}.png").exists() and not (tmp_path / "maps" / f"{g}.json").exists()
    # Decisión 133: uno con positions se las lleva (el MiR las borra con el mapa).
    g = sync.create_map("con positions", _png(), 0.05, 0, 0, session_guid=SES)["guid"]
    p = std_guid("position", 1, parse_std(g)[3], 1)
    (tmp_path / "positions" / f"{p}.json").write_text(json.dumps({
        "guid": p, "name": "X", "type_id": 0, "pos_x": 0, "pos_y": 0,
        "orientation": 0, "map_id": g, "parent_id": None, "docking_offsets": []}))
    assert sync.delete_blockers(g, {}, []) == [] and sync.dependents(g) == [{"guid": p, "kind": "position", "name": "X"}]
    assert sorted(sync.forget(g)) == sorted([g, p])


# --------------------------------------------------------------------- site
def test_crear_site_y_su_primer_mapa(sync, tmp_path):
    r = sync.create_session("nave 3", "planta baja")
    assert r == {"kind": "session", "guid": std_guid("session", 1, 0, 2), "name": "nave 3"}
    doc = Catalog.load(tmp_path).items[r["guid"]]
    assert doc["description"] == "planta baja" and sync.client.writes == []      # solo el catálogo
    with pytest.raises(CreateError, match="ya hay un site 'nave 3'"):
        sync.create_session("nave 3")
    m = sync.create_map("planta baja", _png(), 0.05, 0, 0, session_guid=r["guid"])
    assert m["guid"] == std_guid("map", 1, 2, 2) and not m["new_site"]
    # Sale en «Creado desde aquí», pero no se borra mientras tenga un mapa.
    assert {d["kind"] for d in sync.create_choices()["created"]} == {"session", "map"}
    assert "la usan otros recursos del catálogo: planta baja" in sync.delete_blockers(r["guid"], {}, [])[0]
    sync.forget(m["guid"])
    assert sync.delete_blockers(r["guid"], {}, []) == []



# ------------------------------------------------------- capturar un mapa
ROBOT_SND = "fb225e1c-3d65-11f1-8021-000e8eb5b8fb"      # sonido subido solo a ese robot


def _robot_map(sync):
    """Un mapa hecho en la web del robot (GUIDs del MiR): una position, un
    marcador con su entrada y dos zonas, una con un sonido que no es del catálogo."""
    import base64
    db = sync.client.db
    db["sessions"]["rs"] = {"guid": "rs", "name": "planta"}
    db["maps"]["rm"] = {"guid": "rm", "name": "taller", "session_id": "rs", "resolution": 0.05,
                        "origin_x": -1.0, "origin_y": 2.0, "origin_theta": 0.0,
                        "base_map": base64.b64encode(_png(20, 10)).decode()}
    pos = {"map_id": "rm", "parent_id": None, "orientation": 0.0}
    db["positions"]["rp"] = {**pos, "guid": "rp", "name": "Mesa", "type_id": 0, "pos_x": 1.0, "pos_y": 1.0}
    db["positions"]["rv"] = {**pos, "guid": "rv", "name": "VL", "type_id": 11, "pos_x": 2.0, "pos_y": 1.0}
    db["positions"]["re"] = {**pos, "guid": "re", "name": "VL", "type_id": 12, "pos_x": 3.0, "pos_y": 1.0,
                             "parent_id": "rv"}
    zone = {"map_id": "rm", "polygon": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]}
    db["zones"]["rz"] = {**zone, "guid": "rz", "name": "Pasillo", "type_id": 206, "actions": []}
    db["zones"]["rs1"] = {**zone, "guid": "rs1", "name": "Pitido", "type_id": 102, "actions": [
        {"type_id": 3, "parameters": [{"name": "sound", "value": ROBOT_SND}]}]}


def test_capturar_un_mapa_del_robot_con_positions_y_zonas(sync, tmp_path):
    # Decisión 132: el robot no se toca; todo entra con GUIDs de la flota.
    _robot_map(sync)
    # Un mapa del catálogo capturado a mano de este robot, sin el GUID de
    # origen (los de la fase 0): no debe romper la lista.
    doc = json.loads((tmp_path / "maps" / f"{MAP}.json").read_text())
    (tmp_path / "maps" / f"{MAP}.json").write_text(json.dumps({**doc, "_origen": {"robot": "mir-2"}}))
    maps = sync.capturable_maps()
    assert [(m["name"], m["site"], m["positions"], m["zones"], m["captured_as"]) for m in maps] == \
        [("taller", "planta", 3, 2, None)]                          # el del catálogo (MAP) no sale
    out = sync.capture_map("rm", new_session="planta")
    assert sync.client.writes == []
    assert (out["name"], out["site"], out["new_site"], out["positions"], out["zones"]) == ("taller", "planta", True, 3, 1)
    assert out["meters"] == [1.0, 0.5] and "no está en el catálogo" in out["skipped"][0]
    cat = Catalog.load(tmp_path)
    g = out["guid"]
    assert g == std_guid("map", 1, 2, 2) and cat.items[g]["session_id"] == std_guid("session", 1, 0, 2)
    assert cat.map_png(g) == _png(20, 10) and cat.items[g]["origin_x"] == -1.0
    pos = {d["_origen"]["guid"]: d for d in cat.items.values() if d.get("map_id") == g and cat.kind(d["guid"]) == "position"}
    assert {d["guid"] for d in pos.values()} == {std_guid("position", 1, 2, n) for n in (1, 2, 3)}
    assert pos["re"]["parent_id"] == pos["rv"]["guid"] and pos["re"]["creada_por_mir"]
    zones = [d for d in cat.items.values() if cat.kind(d["guid"]) == "zone" and d["map_id"] == g]
    assert [z["name"] for z in zones] == ["Pasillo"]
    assert cat.closure([g]) == [cat.items[g]["session_id"], g]
    assert sync.capturable_maps()[0]["captured_as"] == g


def test_volver_a_capturar_un_mapa_conserva_sus_guids(sync, tmp_path):
    _robot_map(sync)
    first = sync.capture_map("rm", new_session="planta")
    mesa = next(d["guid"] for d in Catalog.load(tmp_path).items.values() if d["name"] == "Mesa")
    db = sync.client.db
    db["positions"]["rp"]["pos_x"] = 5.0                     # se mueve en la web
    del db["zones"]["rz"]                                    # y se borra una zona
    db["positions"]["rn"] = {"guid": "rn", "name": "Nueva", "type_id": 0, "pos_x": 0, "pos_y": 0,
                             "orientation": 0, "map_id": "rm", "parent_id": None}
    again = sync.capture_map("rm", name="otro nombre", session_guid=SES)   # se ignoran: ya está capturado
    assert again["guid"] == first["guid"] and again["updated"] and again["site"] == "planta"
    assert again["gone"] == ["Pasillo"]                      # se dice; no se borra del catálogo
    cat = Catalog.load(tmp_path)
    assert cat.items[mesa]["pos_x"] == 5.0 and cat.items[first["guid"]]["name"] == "taller"
    assert next(d["guid"] for d in cat.items.values() if d["name"] == "Nueva") == std_guid("position", 1, 2, 4)
    with pytest.raises(CreateError, match="ya tiene GUID de la flota"):
        sync.capture_map(MAP)
