"""Quitar de un robot y retirar de la flota (fm/adapters/mir/remove.py), sin red."""
import json

import pytest

from fm.adapters.mir.sync import Catalog, MirSync, std_guid
from tests.adapters.test_mir_sync import GRP, M_MAIN, M_SUB, MAP, P_MARKER, P_NORMAL, P_OTHER, FakeMir, write_catalog


@pytest.fixture
def sync(tmp_path):
    write_catalog(tmp_path)
    s = MirSync("mir-2", "10.0.0.2", "Basic x", tmp_path)
    s.client = FakeMir()
    s.client.host = "10.0.0.2"
    s.deploy(list(Catalog.load(tmp_path).items))          # el robot tiene todo el catálogo
    return s


def test_retirar_mission_reserva_su_numero(sync, tmp_path):
    assert sync.retire_blockers(M_MAIN, {}, []) == []
    sync.delete(M_MAIN)
    assert sync.forget(M_MAIN, retire=True, robots=["mir-2"]) == [M_MAIN]
    assert M_MAIN not in Catalog.load(tmp_path).items and not sync.exists(M_MAIN)
    log = json.loads((tmp_path / "retirados.json").read_text())
    assert log[0]["guid"] == M_MAIN and log[0]["name"] == "principal" and log[0]["robots"] == ["mir-2"]
    # M_MAIN era la 2 (la más alta): la siguiente mission NO la reutiliza
    sync.client.db["mission_groups"][GRP] = {"guid": GRP, "name": "g"}
    assert sync.create_mission("nueva", GRP)["guid"] == std_guid("mission", 1, 1, 3)


def test_retirar_bloqueos(sync):
    assert "la usan otros recursos del catálogo: principal" in sync.retire_blockers(M_SUB, {}, [])[0]
    assert "fleet.yaml (actions: ir)" in sync.retire_blockers(M_MAIN, {"ir": {"mission": "principal"}}, [])[0]
    assert "allowlist" in sync.retire_blockers(P_OTHER, {"ir": {"mission": "x", "positions_allowlist": ["B"]}}, [])[0]
    assert sync.retire_blockers(P_OTHER, {}, []) == []                   # nadie usa la B
    assert "se retira con su marcador" in sync.retire_blockers(
        next(g for g, d in Catalog.load(sync.catalog_dir).items.items() if d.get("creada_por_mir")), {}, [])[0]


def test_retirar_marcador_se_lleva_su_entrada(sync, tmp_path):
    sync.delete(M_MAIN)
    sync.forget(M_MAIN, retire=True)                       # la mission que lo usaba, antes
    assert sync.retire_blockers(P_MARKER, {}, []) == []
    sync.delete(P_MARKER)
    gone = sync.forget(P_MARKER, retire=True, robots=["mir-2"])
    assert len(gone) == 2 and {r["guid"] for r in json.loads((tmp_path / "retirados.json").read_text())} >= set(gone)


def test_quitar_de_un_robot(sync, tmp_path):
    sync._wanted = []                                                      # fleet.yaml no pide nada
    assert sync.remove_blockers(M_SUB) == ["la usan en este robot: principal"]
    assert sync.remove_blockers(M_MAIN) == []
    sync.delete(M_MAIN)
    assert M_MAIN in Catalog.load(tmp_path).items                          # el catálogo no cambia
    assert sync.remove_blockers(M_MAIN) == ["este robot no la tiene"]
    assert sync.remove_blockers(M_SUB) == []                               # ya nadie la usa aquí
    sync._wanted = ["principal"]
    assert "la necesita este robot" in sync.remove_blockers(M_SUB)[0]      # la pide (vía principal)
    assert "una position se retira" in sync.remove_blockers(P_NORMAL)[0]


def test_sobrantes_en_la_comprobacion(sync):
    sync._wanted = ["sub"]
    r = sync.check()
    assert r["surplus"] == [M_MAIN]                                         # solo missions, y no las pedidas



def test_retirar_un_mapa_se_lleva_sus_positions_y_zonas(sync, tmp_path):
    # Decisión 133. Mientras una mission vaya a sus positions, no.
    assert "la usan otros recursos del catálogo: principal, sub" in sync.retire_blockers(MAP, {}, [])[0]
    for m in (M_MAIN, M_SUB):
        sync.delete(m)
        sync.forget(m, retire=True)
    allow = {"ir": {"mission": "x", "positions_allowlist": ["B"]}}
    assert "allowlist de fleet.yaml (actions: ir)" in sync.retire_blockers(MAP, allow, [])[0]
    assert sync.retire_blockers(MAP, {}, []) == []
    assert {d["name"] for d in sync.dependents(MAP)} == {"A", "VL", "B"}       # VL dos veces: marcador y entrada
    sync.delete(MAP)                                       # en el robot se van con él
    assert not sync.client.db["positions"] and not sync.exists(MAP)
    gone = sync.forget(MAP, retire=True, robots=["mir-2"])
    cat = Catalog.load(tmp_path)
    assert len(gone) == 5 and MAP not in cat.items and not any(cat.kind(g) == "position" for g in cat.items)
    assert not (tmp_path / "maps" / f"{MAP}.png").exists()
    assert {r["guid"] for r in json.loads((tmp_path / "retirados.json").read_text())} >= set(gone)
