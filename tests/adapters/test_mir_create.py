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
    # Un grupo del catálogo que el robot aún no tiene se crea en él con la mission.
    del sync.client.db["mission_groups"][GRP]
    sync.create_mission("x", GRP)
    assert sync.client.db["mission_groups"][GRP]["name"] == "g"


def test_crear_position_donde_esta_el_robot(sync, tmp_path):
    r = sync.create_position("Mesa nueva", MAP)
    assert r["guid"] == std_guid("position", 1, 1, 5)             # el catálogo tiene 1..4 en ese mapa
    assert r["url"] == f"http://10.0.0.2/maps/{MAP}"
    p = sync.client.db["positions"][r["guid"]]
    assert (p["type_id"], p["pos_x"], p["pos_y"], p["orientation"]) == (0, 1.235, 4.5, 89.99)
    assert r["guid"] in Catalog.load(tmp_path).drafts
    with pytest.raises(CreateError, match=r"ya hay una position con ese nombre en el catálogo \(mapa 'm'\)"):
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
    assert "Crear o importar recurso" in sync.delete_blockers(M_MAIN, {}, [])[0]     # capturada con el script
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



# -------------------------------------------------------------------- grupo
def test_crear_grupo_y_su_primera_mission(sync, tmp_path):
    r = sync.create_group("pruebas")
    g = std_guid("missiongrp", 1, 0, 2)
    assert r == {"kind": "missiongrp", "guid": g, "name": "pruebas"} and sync.client.writes == []  # solo el catálogo
    with pytest.raises(CreateError, match="ya hay un grupo 'pruebas'"):
        sync.create_group("pruebas")
    # La primera mission no tiene de quién copiar la session: va al site de la flota.
    with pytest.raises(CreateError, match="no hay site de la flota"):
        sync.create_mission("primera", g)
    m = sync.create_mission("primera", g, site=SES)["guid"]
    assert sync.client.db["mission_groups"][g]["name"] == "pruebas"            # llega al robot con ella
    assert sync.client.db["missions"][m]["session_id"] == SES
    assert sync.client.db["missions"][sync.create_mission("segunda", g)["guid"]]["session_id"] == SES
    assert {d["kind"] for d in sync.create_choices()["created"]} >= {"missiongrp", "mission"}
    # No se borra con missions dentro: borradores, capturadas o hechas en la web del robot.
    assert "tiene missions en borrador: primera, segunda" in sync.delete_blockers(g, {}, [])[0]
    sync.capture(m)
    assert "la usan otros recursos del catálogo: primera" in " ".join(sync.delete_blockers(g, {}, []))
    assert "primera, segunda" in sync.in_use_here(g)[0]
    for x in [x for x, d in sync.client.db["missions"].items() if d["group_id"] == g]:
        sync.delete(x)
        sync.forget(x)
    assert sync.delete_blockers(g, {}, []) == [] and sync.in_use_here(g) == []
    sync.delete(g)
    assert sync.forget(g) == [g] and sync.create_group("otro")["guid"] == g    # era el más alto: queda libre


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


def test_nombre_de_position_unico_en_todos_los_mapas_y_robots(sync):
    # Decisión 174: la web del MiR lista las positions de todos los mapas sin
    # decir de cuál es cada una; un nombre repetido lleva a elegir la que no es.
    sync.client.db["maps"]["otro"] = {"guid": "otro", "name": "suberri"}
    sync.client.db["positions"]["x1"] = {"guid": "x1", "name": "Area_calidad", "type_id": 0,
                                         "map_id": "otro", "parent_id": None}
    with pytest.raises(CreateError, match=r"mir-2 \(mapa 'suberri'\)"):
        sync.create_position("Area_calidad", MAP)              # de otro mapa del mismo robot
    with pytest.raises(CreateError, match=r"\('Area_calidad'\)"):
        sync.create_position("  area_CALIDAD ", MAP)            # mayúsculas y espacios no cuentan
    otros = {"montaje": ("Montaje", "mir-1 (mapa 'suberri')")}
    with pytest.raises(CreateError, match=r"mir-1 \(mapa 'suberri'\)"):
        sync.create_position("Montaje", MAP, other_names=otros)  # de otro robot
    assert sync.create_position("Montaje 2", MAP, other_names=otros)["name"] == "Montaje 2"


# ------------------------------------------------------ copiar un mapa (#33)
def _zone_on(tmp_path, map_guid, n=1, name="Espera", actions=()):
    z = std_guid("zone", 1, parse_std(map_guid)[3], n)
    (tmp_path / "zones").mkdir(exist_ok=True)
    (tmp_path / "zones" / f"{z}.json").write_text(json.dumps({
        "guid": z, "name": name, "type_id": 102, "shape_type": "shape", "stroke_width": 0.0, "direction": 0.0,
        "polygon": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}], "actions": list(actions),
        "map_id": map_guid, "_origen": {"robot": "mir-1"}}))
    return z


def test_copiar_un_mapa_con_positions_y_zonas(sync, tmp_path):
    from tests.adapters.test_mir_sync import P_ENTRY, P_MARKER, PNG
    sound = [{"type_id": 3, "parameters": [{"name": "sound", "value": SND}]}]
    _zone_on(tmp_path, MAP, actions=sound)
    out = sync.copy_map(MAP, "m-pruebas", "-pruebas", "suffix", others={std_guid("map", 1, 1, 2)})
    assert sync.client.writes == []                               # solo el catálogo
    g = out["guid"]
    assert g == std_guid("map", 1, 1, 3)                          # el 2 lo tiene un robot
    assert (out["site"], out["copied_from"], out["positions"], out["zones"]) == ("s", "m", 3, 1)
    assert out["example"] == ["A", "A-pruebas"]
    cat = Catalog.load(tmp_path)
    m = cat.items[g]
    assert (m["session_id"], m["origin_x"], m["origin_y"], m["resolution"]) == (SES, -1.0, -2.0, 0.05)
    assert cat.map_png(g) == PNG
    pos = {d["_origen"]["copia_de"]: d for d in cat.items.values()
           if cat.kind(d["guid"]) == "position" and d["map_id"] == g}
    assert {d["guid"] for d in pos.values()} == {std_guid("position", 1, 3, n) for n in (1, 2, 3, 4)}
    assert sorted(d["name"] for d in pos.values()) == ["A-pruebas", "B-pruebas", "VL-pruebas", "VL-pruebas"]
    # La entrada del marcador va con su madre copiada, y conserva el offset.
    assert pos[P_ENTRY]["parent_id"] == pos[P_MARKER]["guid"] and pos[P_ENTRY]["creada_por_mir"]
    assert pos[P_MARKER]["docking_offsets"] == [{"x_offset": -1.0, "y_offset": 0.1, "orientation_offset": 2.0}]
    zone = next(d for d in cat.items.values() if cat.kind(d["guid"]) == "zone" and d["map_id"] == g)
    assert zone["name"] == "Espera" and zone["actions"] == sound    # la zona conserva nombre y sonido
    # Sincronización lo despliega con su site; en Crear sale el mapa, no sus positions.
    assert cat.closure([g]) == [SES, g]
    assert [(d["kind"], d["guid"]) for d in sync.create_choices()["created"]] == [("map", g)]
    # El original, intacto.
    assert sum(1 for d in cat.items.values() if d.get("map_id") == MAP) == 5


def test_copiar_un_mapa_con_prefijo_y_a_otro_site(sync, tmp_path):
    ses2 = sync.create_session("nave 2")["guid"]
    out = sync.copy_map(MAP, "m2", "n2 ", "prefix", session_guid=ses2)
    assert out["site"] == "nave 2" and out["example"] == ["A", "n2 A"]
    assert out["guid"] == std_guid("map", 1, 2, 2)                 # padre = su site
    # Borrarlo (decisión 133) se lleva sus positions y zonas, no las del original.
    assert sync.delete_blockers(out["guid"], {}, []) == []
    assert len(sync.forget(out["guid"])) == 5
    assert sum(1 for d in Catalog.load(tmp_path).items.values() if d.get("map_id") == MAP) == 4


def test_copiar_un_mapa_errores(sync):
    with pytest.raises(CreateError, match="falta el texto"):
        sync.copy_map(MAP, "copia", "  ", "suffix")
    with pytest.raises(CreateError, match="prefijo o sufijo"):
        sync.copy_map(MAP, "copia", "x", "medio")
    with pytest.raises(CreateError, match="ya hay un mapa 'M'"):
        sync.copy_map(MAP, "M", "x", "suffix")                    # mayúsculas no cuentan
    with pytest.raises(CreateError, match="no está en el catálogo"):
        sync.copy_map("imperialfleet-map0000000-01-0001-099", "copia", "x", "suffix")
    # Decisión 174: el nombre nuevo no puede estar ya en el catálogo ni en un robot.
    sync.create_position("B2", MAP)
    with pytest.raises(CreateError, match=r"«B2» \(ya está en el catálogo \(mapa 'm'\)\)"):
        sync.copy_map(MAP, "copia", "2", "suffix")
    otros = {"a-x": ("A-x", "mir-1 (mapa 'suberri')")}
    with pytest.raises(CreateError, match=r"«A-x» \(ya está en mir-1"):
        sync.copy_map(MAP, "copia", "-x", "suffix", other_names=otros)
    out = sync.copy_map(MAP, "copia", "-y", "suffix", other_names=otros)
    assert out["positions"] == 3                                  # A, VL y B: el borrador B2 no se copia


# ------------------- traer lo hecho en la web del robot, con GUID del proyecto (#19)
def _web_position(sync, guid="rw", name="Mesa web", type_id=11):
    """Una position hecha en la web del robot sobre el mapa del catálogo; si es
    un marcador, con la entrada que le pone el MiR."""
    db = sync.client.db
    db["positions"][guid] = {"guid": guid, "name": name, "type_id": type_id, "pos_x": 2.0, "pos_y": 3.0,
                             "orientation": 45.0, "map_id": MAP, "parent_id": None}
    if type_id == 11:
        db["positions"][guid + "e"] = {"guid": guid + "e", "name": name, "type_id": 12, "pos_x": 3.0, "pos_y": 3.0,
                                       "orientation": 45.0, "map_id": MAP, "parent_id": guid}


def test_lista_lo_hecho_en_la_web_dentro_de_lo_de_la_flota(sync):
    _web_position(sync)
    _web_mission(sync, "hecha en la web")
    sync.client.db["zones"]["rz"] = {"guid": "rz", "name": "Pasillo", "type_id": 206, "map_id": MAP, "actions": [],
                                     "polygon": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]}
    sync.client.db["positions"]["fuera"] = {"guid": "fuera", "name": "otra", "type_id": 0, "map_id": "otro-mapa",
                                            "parent_id": None}
    got = sorted((x["kind"], x["name"], x["where"]) for x in sync.foreign())
    # La entrada del marcador no sale (va con él), ni lo que está en un mapa que no es del catálogo.
    assert got == [("mission", "hecha en la web", "g"), ("position", "Mesa web", "m"), ("zone", "Pasillo", "m")]


def test_traer_un_marcador_lo_sustituye_en_el_robot(sync, tmp_path):
    _web_position(sync)
    out = sync.bring("position", "rw")
    g = std_guid("position", 1, 1, 5)                    # el catálogo tiene 1..4 en ese mapa
    assert (out["guid"], out["replaced"], out["name"]) == (g, True, "Mesa web")
    cat = Catalog.load(tmp_path)
    doc = cat.items[g]
    assert (doc["type_id"], doc["pos_x"], doc["map_id"], doc["_origen"]["guid"]) == (11, 2.0, MAP, "rw")
    entry = next(d for d in cat.items.values() if d.get("parent_id") == g)
    assert entry["creada_por_mir"] and entry["guid"] == std_guid("position", 1, 1, 6)
    db = sync.client.db["positions"]
    assert "rw" not in db and "rwe" not in db             # el original y su entrada, borrados
    assert db[g]["name"] == "Mesa web"                   # y la copia con el GUID del proyecto
    # No sale en «Creado desde aquí»: no se creó desde el FM.
    assert g not in {d["guid"] for d in sync.create_choices()["created"]}
    assert sync.foreign() == []


def test_traer_no_toca_nada_si_algo_del_robot_lo_usa(sync, tmp_path):
    _web_position(sync, type_id=0)
    _web_mission(sync, "va a la mesa", {"action_type": "move", "priority": 1, "parameters": [
        {"id": "position", "value": "rw", "input_name": None}]})
    before = set(Catalog.load(tmp_path).items)
    with pytest.raises(CreateError, match="la usa 'va a la mesa'"):
        sync.bring("position", "rw")
    assert set(Catalog.load(tmp_path).items) == before and "rw" in sync.client.db["positions"]
    # Ni si es la mission de una action de fleet.yaml por su GUID propio.
    m = next(g for g, d in sync.client.db["missions"].items() if d["name"] == "va a la mesa")
    with pytest.raises(CreateError, match="fleet.yaml"):
        sync.bring("mission", m, config_refs={m})


def test_traer_position_con_nombre_repetido(sync):
    _web_position(sync, name="A", type_id=0)              # «A» ya está en el catálogo
    with pytest.raises(CreateError, match=r"ya hay una position 'A' en el catálogo"):
        sync.bring("position", "rw")
    _web_position(sync, guid="rx", name="Montaje", type_id=0)
    otros = {"montaje": ("Montaje", "mir-1 (mapa 'suberri')")}
    with pytest.raises(CreateError, match=r"mir-1 \(mapa 'suberri'\)"):
        sync.bring("position", "rx", other_names=otros)
    # El original y su entrada en este robot no cuentan como repetidos.
    _web_position(sync, guid="ry", name="Única")
    assert sync.bring("position", "ry")["replaced"]


def test_traer_zona_y_mission(sync, tmp_path):
    sync.client.db["zones"]["rz"] = {"guid": "rz", "name": "Pasillo", "type_id": 206, "map_id": MAP, "actions": [],
                                     "polygon": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]}
    z = sync.bring("zone", "rz")
    assert z["guid"] == std_guid("zone", 1, 1, 1) and "rz" not in sync.client.db["zones"]
    assert sync.client.db["zones"][z["guid"]]["name"] == "Pasillo"
    m = _web_mission(sync, "nueva web", {"action_type": "move", "priority": 1, "parameters": [
        {"id": "position", "value": P_NORMAL, "input_name": "destino"}]})
    out = sync.bring("mission", m)
    assert out["replaced"] and m not in sync.client.db["missions"]
    doc = Catalog.load(tmp_path).items[out["guid"]]
    assert doc["group_id"] == GRP and doc["session_id"] == SES and doc["actions"][0]["parameters"][0]["value"] == P_NORMAL
    assert sync.client.db["missions"][out["guid"]]["name"] == "nueva web"
    # Con el nombre de una del catálogo en el mismo grupo: eso es «Adoptar».
    m2 = _web_mission(sync, "principal")
    with pytest.raises(CreateError, match="Adoptar"):
        sync.bring("mission", m2)
    with pytest.raises(CreateError, match="ya tiene GUID de la flota"):
        sync.bring("mission", M_MAIN)


# ---------------------- traer lo de la flota editado en la web de un robot (#19)
def test_recapturar_un_marcador_movido_en_el_robot(sync, tmp_path):
    from tests.adapters.test_mir_sync import P_ENTRY, P_MARKER
    sync.deploy([P_MARKER])
    db = sync.client.db["positions"]
    db[P_MARKER].update(pos_x=5.5, orientation=170.0)      # recalibrado en la web de mir-2
    entry = next(g for g, p in db.items() if p.get("parent_id") == P_MARKER)
    db[entry]["pos_x"] = 6.4
    out = sync.recapture(P_MARKER)
    # (El catálogo de prueba guarda el offset solo con x/y/orientación; al traerlo llegan todas sus claves.)
    assert out["changed"] == ["docking_offsets", "orientation", "pos_x", "entrada"]
    assert ("DELETE", f"positions/{P_MARKER}") not in sync.client.writes          # solo lee el robot
    cat = Catalog.load(tmp_path)
    d = cat.items[P_MARKER]
    assert (d["pos_x"], d["orientation"], cat.items[P_ENTRY]["pos_x"]) == (5.5, 170.0, 6.4)
    assert d["_origen"] == {"robot": "mir-2"} and d["_recapturado"]["robot"] == "mir-2"   # el origen no cambia
    _, ops = sync.preview([P_MARKER])
    assert {o.verb for o in ops if o.kind == "position"} == {"igual"}             # ya coincide con el robot
    # La entrada movida en la web sale «corregir»; traerla trae su marcador con ella.
    db[entry]["pos_x"] = 6.9
    out = sync.recapture(P_ENTRY)
    assert (out["guid"], out["changed"]) == (P_MARKER, ["entrada"])
    assert Catalog.load(tmp_path).items[P_ENTRY]["pos_x"] == 6.9


def test_recapturar_mission_y_mapa(sync, tmp_path):
    import base64
    sync.deploy([M_SUB])
    acts = [a for a in sync.client.db["actions"].values() if a["mission_id"] == M_SUB]
    for a in acts:                                          # se quita el sonido en la web
        if a["action_type"] == "sound":
            del sync.client.db["actions"][a["guid"]]
    out = sync.recapture(M_SUB)
    assert out["changed"] == ["actions"] and len(Catalog.load(tmp_path).items[M_SUB]["actions"]) == 1
    m = sync.client.db["maps"][MAP]
    m.update(name="m", session_id=SES, resolution=0.05, origin_x=-1.5, origin_y=-2.0, origin_theta=0.0,
             base_map=base64.b64encode(_png(6, 6)).decode())
    out = sync.recapture(MAP)
    assert out["changed"] == ["origin_x", "imagen"]
    cat = Catalog.load(tmp_path)
    assert cat.items[MAP]["origin_x"] == -1.5 and cat.map_png(MAP) == _png(6, 6)
    with pytest.raises(CreateError, match="solo missions, positions, zonas y mapas"):
        sync.recapture(GRP)
