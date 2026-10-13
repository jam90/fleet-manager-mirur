"""Despliega las positions de un mapa del catálogo (`catalog/`) en un MiR.

Uso:
    catalog_deploy.py <serial> <guid_mapa_catalogo> [--escribir]

Sin `--escribir` solo enseña el plan. El mapa tiene que existir ya en el
robot con ese GUID. Idempotente: lo que ya está igual no se toca.

- Position normal o marcador/cargador: se crea con el GUID del catálogo. Un
  marcador lleva sus `offset_*` (= su docking offset), que el MiR exige.
- Entrada de marcador/cargador (`creada_por_mir`): la crea el MiR al crear
  la madre, con su propio GUID. Se busca por `parent_id` y, si no está donde
  dice el catálogo, se corrige con PUT (decisión 77 de docs/avance.md).
"""
from __future__ import annotations

import argparse
import json
import sys

from _common import ROOT, client_for, load, robot_or_exit

from fm.adapters.mir.client import MirApiError

POSE = ("pos_x", "pos_y", "orientation")
TOL = 1e-3


def same_pose(a: dict, b: dict) -> bool:
    return all(abs(a[k] - b[k]) < TOL for k in POSE)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("serial")
    ap.add_argument("guid_mapa")
    ap.add_argument("--escribir", action="store_true")
    a = ap.parse_args()

    files = sorted((ROOT / "catalog" / "positions").glob("*.json"))
    cat = [d for d in (json.loads(f.read_text(encoding="utf-8")) for f in files)
           if d["map_id"] == a.guid_mapa]
    if not cat:
        sys.exit(f"el catálogo no tiene positions del mapa {a.guid_mapa}")

    c = client_for(robot_or_exit(load(), a.serial))
    try:
        c._req("GET", f"maps/{a.guid_mapa}")
    except MirApiError:
        sys.exit(f"{a.serial} no tiene el mapa {a.guid_mapa}: despliégalo antes")

    def robot_positions() -> dict[str, dict]:
        return {p["guid"]: c._req("GET", f"positions/{p['guid']}")
                for p in c._req("GET", f"maps/{a.guid_mapa}/positions")}

    en_robot = robot_positions()
    cambios = 0
    # El catálogo va en orden de número: la madre siempre antes que su entrada.
    for d in cat:
        tag = f"{d['guid'][-3:]} {d['name']!r} (tipo {d['type_id']})"
        if d.get("creada_por_mir"):
            hijas = [p for p in en_robot.values() if p["parent_id"] == d["parent_id"]]
            if not hijas:
                print(f"  {tag}: entrada aún no creada (se crea con su madre)")
                continue
            h = hijas[0]
            if same_pose(h, d):
                print(f"= {tag}: entrada ya en su sitio ({h['guid']})")
                continue
            print(f"~ {tag}: mover entrada {h['guid']} "
                  f"({h['pos_x']:.3f},{h['pos_y']:.3f},{h['orientation']}) → "
                  f"({d['pos_x']},{d['pos_y']},{d['orientation']})")
            if a.escribir:
                c._req("PUT", f"positions/{h['guid']}", json={k: d[k] for k in POSE})
            cambios += 1
            continue

        if d["guid"] in en_robot:
            ok = same_pose(en_robot[d["guid"]], d) and en_robot[d["guid"]]["name"] == d["name"]
            print(f"{'=' if ok else '!'} {tag}: ya existe"
                  + ("" if ok else " PERO DISTINTA (no se toca; sustituir queda para el FM)"))
            continue
        body = {k: d[k] for k in ("guid", "name", "type_id", *POSE, "map_id")}
        if d["docking_offsets"]:
            o = d["docking_offsets"][0]
            body |= {"offset_x": o["x_offset"], "offset_y": o["y_offset"],
                     "offset_orientation": o["orientation_offset"]}
        print(f"+ {tag}: crear")
        cambios += 1
        if a.escribir:
            try:
                c._req("POST", "positions", json=body)
            except MirApiError as e:
                print(f"  RECHAZADA {e.status}: {json.dumps(e.body, ensure_ascii=False)}")
                continue
            # La madre puede haber creado su entrada: refrescar para el siguiente paso.
            en_robot = robot_positions()

    print(f"\n{cambios} cambios" + ("" if a.escribir else " (prueba: añade --escribir)"))


if __name__ == "__main__":
    main()
