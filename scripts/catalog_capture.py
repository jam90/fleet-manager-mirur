"""Captura positions de un mapa de un MiR al catálogo (`catalog/`), con GUID estándar.

Uso:
    catalog_capture.py <serial> <mapa_origen> <guid_mapa_catalogo> [--escribir]

Ejemplo (fase 0 de docs/plan-admin.md):
    catalog_capture.py mir-2 suberri imperialfleet-map0000000-01-0001-001 --escribir

Lee las positions de <mapa_origen> en el robot y las guarda como
`catalog/positions/<guid>.json`, colgando del mapa del catálogo. Sin
`--escribir` solo enseña lo que haría. Solo hace GET al robot.

Formato del GUID: docs/plan-admin.md §4. Las positions se numeran dentro de
su mapa, en el orden en que las devuelve el robot (= orden de creación).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from _common import ROOT, client_for, load, robot_or_exit

CATALOG = ROOT / "catalog"
PREFIX = "imperialfleet"
# Tipos de position que el MiR crea solo al crear su madre (entrada de
# marcador VL = 12, de cargador = 21).
AUTO_CHILD_TYPES = (12, 21)


def std_guid(tipo: str, inst: int, padre: int, numero: int) -> str:
    """GUID estándar: `imperialfleet-<tipo relleno a 10>-II-PPPP-NNN` (36 caracteres)."""
    g = f"{PREFIX}-{tipo.ljust(10, '0')}-{inst:02d}-{padre:04d}-{numero:03d}"
    assert len(g) == 36, g
    return g


def parse_std(guid: str) -> tuple[str, int, int, int]:
    """Inversa de std_guid: (tipo, inst, padre, número)."""
    _, tipo, inst, padre, numero = guid.split("-")
    return tipo.rstrip("0"), int(inst), int(padre), int(numero)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("serial")
    ap.add_argument("mapa_origen", help="nombre del mapa en el robot")
    ap.add_argument("guid_mapa", help="GUID estándar del mapa del catálogo al que cuelgan")
    ap.add_argument("--escribir", action="store_true")
    a = ap.parse_args()

    _, inst, _, map_num = parse_std(a.guid_mapa)
    c = client_for(robot_or_exit(load(), a.serial))
    maps = {m["name"]: m["guid"] for m in c._req("GET", "maps")}
    if a.mapa_origen not in maps:
        sys.exit(f"'{a.mapa_origen}' no está en {a.serial}; mapas: {sorted(maps)}")
    src_map = maps[a.mapa_origen]

    positions = [c._req("GET", f"positions/{p['guid']}")
                 for p in c._req("GET", f"maps/{src_map}/positions")]
    # GUID del robot → GUID estándar. Primero todas, para poder traducir parent_id.
    new = {p["guid"]: std_guid("position", inst, map_num, i)
           for i, p in enumerate(positions, start=1)}

    out = CATALOG / "positions"
    for p in positions:
        g = new[p["guid"]]
        # Docking offset (calibración del marcador): 1 por position. Sin GUID
        # propio: el MiR lo crea solo al crear el marcador (decisión 77).
        offsets = [c._req("GET", f"docking_offsets/{o['guid']}")
                   for o in c._req("GET", f"positions/{p['guid']}/docking_offsets")]
        assert len(offsets) <= 1, (p["name"], len(offsets))
        doc = {
            "guid": g,
            "name": p["name"],
            "type_id": p["type_id"],
            "pos_x": p["pos_x"],
            "pos_y": p["pos_y"],
            "orientation": p["orientation"],
            "map_id": a.guid_mapa,
            "parent_id": new.get(p["parent_id"]) if p["parent_id"] else None,
            "docking_offsets": [
                {k: o[k] for k in ("docking_type", "name", "x_offset", "y_offset",
                                   "orientation_offset", "bar_length", "bar_distance",
                                   "shelf_leg_asymmetry_x")}
                for o in offsets],
            # Entrada de marcador/cargador: la crea el MiR con su propio GUID.
            # El nuestro solo identifica el fichero; en el robot se busca por
            # parent_id (decisión 77).
            "creada_por_mir": p["type_id"] in AUTO_CHILD_TYPES,
            "_origen": {"robot": a.serial, "mapa": a.mapa_origen, "guid": p["guid"],
                        "capturado": date.today().isoformat()},
        }
        hijo = f" (hija de {doc['parent_id'][-3:]})" if doc["parent_id"] else ""
        print(f"{g}  tipo {p['type_id']:>2}  {p['name']}{hijo}"
              + ("  + docking offset" if offsets else ""))
        if a.escribir:
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{g}.json").write_text(
                json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\n{len(positions)} positions" + (f" escritas en {out}" if a.escribir else
                                              " (prueba: añade --escribir)"))


if __name__ == "__main__":
    main()
