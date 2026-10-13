"""Despliega recursos del catálogo (`catalog/`) en un MiR.

Uso:
    catalog_deploy.py positions <serial> <guid_mapa> [--escribir]
    catalog_deploy.py missions  <serial> <guid_grupo> [--escribir]

Sin `--escribir` solo enseña el plan. Idempotente: lo que ya está igual no
se toca, y lo que existe pero es distinto se avisa y **no** se toca
(sustituir queda para el FM, con vista previa).

positions — el mapa tiene que existir ya en el robot con ese GUID.
- Position normal o marcador/cargador: se crea con el GUID del catálogo. Un
  marcador lleva sus `offset_*` (= su docking offset), que el MiR exige.
- Entrada de marcador/cargador (`creada_por_mir`): la crea el MiR al crear
  la madre, con su propio GUID. Se busca por `parent_id` y, si no está donde
  dice el catálogo, se corrige con PUT (decisión 77 de docs/avance.md).

missions — las del grupo del catálogo, con lo que necesitan:
- Sonidos subidos a mano que usen, y el propio grupo, si faltan.
- Las missions en orden de dependencias (la llamada por `load_mission`
  antes que la que llama). Una mission que use una position o mission que
  no esté en el robot no se crea.
- Actions una a una en orden de `priority`; el `scope_reference` de las
  anidadas se rehace con los GUIDs que el MiR pone a los parámetros de la
  contenedora (decisión 78).
"""
from __future__ import annotations

import argparse
import base64
import json
import sys

from _common import ROOT, client_for, load, robot_or_exit

from fm.adapters.mir.client import MirApiError

CATALOG = ROOT / "catalog"
POSE = ("pos_x", "pos_y", "orientation")
TOL = 1e-3
PREFIX = "imperialfleet-"


def read_catalog(folder: str) -> dict[str, dict]:
    return {d["guid"]: d for d in (json.loads(f.read_text(encoding="utf-8"))
                                   for f in sorted((CATALOG / folder).glob("*.json")))}


def same_pose(a: dict, b: dict) -> bool:
    return all(abs(a[k] - b[k]) < TOL for k in POSE)


class Robot:
    """Acceso mínimo al MiR con modo prueba (sin --escribir no se escribe)."""

    def __init__(self, serial: str, escribir: bool):
        self.c = client_for(robot_or_exit(load(), serial))
        self.escribir = escribir
        self.cambios = 0

    def get(self, path: str):
        return self.c._req("GET", path)

    def exists(self, path: str) -> bool:
        try:
            self.get(path)
            return True
        except MirApiError as e:
            if e.status == 404:
                return False
            raise

    def write(self, method: str, path: str, body: dict):
        """Escribe (o solo cuenta, en prueba). Devuelve la respuesta o None."""
        self.cambios += 1
        if not self.escribir:
            return None
        return self.c._req(method, path, json=body)

    def fin(self) -> None:
        print(f"\n{self.cambios} cambios" + ("" if self.escribir else " (prueba: añade --escribir)"))


# ------------------------------------------------------------------ positions
def deploy_positions(a: argparse.Namespace) -> None:
    cat = [d for d in read_catalog("positions").values() if d["map_id"] == a.guid_mapa]
    if not cat:
        sys.exit(f"el catálogo no tiene positions del mapa {a.guid_mapa}")
    r = Robot(a.serial, a.escribir)
    if not r.exists(f"maps/{a.guid_mapa}"):
        sys.exit(f"{a.serial} no tiene el mapa {a.guid_mapa}: despliégalo antes")

    def robot_positions() -> dict[str, dict]:
        return {p["guid"]: r.get(f"positions/{p['guid']}")
                for p in r.get(f"maps/{a.guid_mapa}/positions")}

    en_robot = robot_positions()
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
            r.write("PUT", f"positions/{h['guid']}", {k: d[k] for k in POSE})
            continue

        if d["guid"] in en_robot:
            ok = same_pose(en_robot[d["guid"]], d) and en_robot[d["guid"]]["name"] == d["name"]
            print(f"{'=' if ok else '!'} {tag}: ya existe"
                  + ("" if ok else " PERO DISTINTA (no se toca)"))
            continue
        body = {k: d[k] for k in ("guid", "name", "type_id", *POSE, "map_id")}
        if d["docking_offsets"]:
            o = d["docking_offsets"][0]
            body |= {"offset_x": o["x_offset"], "offset_y": o["y_offset"],
                     "offset_orientation": o["orientation_offset"]}
        print(f"+ {tag}: crear")
        try:
            if r.write("POST", "positions", body) is not None:
                # La madre puede haber creado su entrada: refrescar.
                en_robot = robot_positions()
        except MirApiError as e:
            print(f"  RECHAZADA {e.status}: {json.dumps(e.body, ensure_ascii=False)}")
    r.fin()


# ------------------------------------------------------------------- missions
def robot_actions(r: Robot, mission_guid: str) -> list[dict]:
    """Actions de una mission del robot, normalizadas como en el catálogo."""
    acts = sorted((r.get(f"missions/{mission_guid}/actions/{x['guid']}")
                   for x in r.get(f"missions/{mission_guid}/actions")),
                  key=lambda x: x["priority"])
    owner = {p["guid"]: (x["priority"], p["id"]) for x in acts for p in x["parameters"]}
    return [{"priority": x["priority"], "action_type": x["action_type"],
             "scope": ({"action": owner[x["scope_reference"]][0],
                        "parameter": owner[x["scope_reference"]][1]}
                       if x["scope_reference"] else None),
             "parameters": [{"id": p["id"], "value": p["value"], "input_name": p["input_name"]}
                            for p in x["parameters"]]}
            for x in acts]


def refs(m: dict, prefix: str) -> set[str]:
    """GUIDs del catálogo de un tipo (`…-position00-`) que usa una mission."""
    return {p["value"] for x in m["actions"] for p in x["parameters"]
            if isinstance(p["value"], str) and p["value"].startswith(PREFIX + prefix)}


def deploy_missions(a: argparse.Namespace) -> None:
    groups, missions, sounds = (read_catalog("mission_groups"), read_catalog("missions"),
                                read_catalog("sounds"))
    if a.guid_grupo not in groups:
        sys.exit(f"el catálogo no tiene el grupo {a.guid_grupo}")
    r = Robot(a.serial, a.escribir)

    # Missions del grupo + las que llaman (de cualquier grupo), deps primero.
    orden: list[str] = []

    def visit(g: str, camino: tuple = ()) -> None:
        if g in orden:
            return
        if g in camino:
            sys.exit(f"load_mission circular: {' → '.join(camino + (g,))}")
        for dep in sorted(refs(missions[g], "mission000")):
            visit(dep, camino + (g,))
        orden.append(g)
    for g, m in missions.items():
        if m["group_id"] == a.guid_grupo:
            visit(g)

    # 1. Sonidos y grupos.
    for sg in sorted(set().union(*(refs(missions[g], "sound00000") for g in orden))):
        s = sounds[sg]
        if r.exists(f"sounds/{sg}"):
            print(f"= sonido {s['name']!r}: ya existe")
            continue
        print(f"+ sonido {s['name']!r}: crear")
        audio = base64.b64encode((CATALOG / "sounds" / s["sound"]).read_bytes()).decode()
        r.write("POST", "sounds", {"guid": sg, "name": s["name"], "note": s["note"],
                                   "volume": s["volume"], "sound": audio})
    for gg in sorted({missions[g]["group_id"] for g in orden}):
        if r.exists(f"mission_groups/{gg}"):
            print(f"= grupo {groups[gg]['name']!r}: ya existe")
            continue
        print(f"+ grupo {groups[gg]['name']!r}: crear")
        r.write("POST", "mission_groups", {"guid": gg, "name": groups[gg]["name"],
                                           "priority": 0, "feature": "default", "icon": ""})

    # 2. Missions.
    creadas: set[str] = set()
    for g in orden:
        m = missions[g]
        tag = f"{g[-3:]} {m['name']!r}"
        if r.exists(f"missions/{g}"):
            rm = r.get(f"missions/{g}")
            ok = (rm["name"] == m["name"] and rm["group_id"] == m["group_id"]
                  and robot_actions(r, g) == m["actions"])
            print(f"{'=' if ok else '!'} {tag}: ya existe" + ("" if ok else " PERO DISTINTA (no se toca)"))
            continue
        faltan = [p for p in sorted(refs(m, "position00")) if not r.exists(f"positions/{p}")]
        faltan += [x for x in sorted(refs(m, "mission000"))
                   if x not in creadas and not r.exists(f"missions/{x}")]
        if faltan:
            print(f"X {tag}: NO se crea, faltan en el robot: {faltan}")
            continue
        print(f"+ {tag}: crear ({len(m['actions'])} actions)")
        creadas.add(g)
        try:
            r.write("POST", "missions", {k: m[k] for k in
                                         ("guid", "name", "description", "group_id", "session_id", "hidden")})
            param_guid: dict[tuple[int, str], str] = {}
            for x in m["actions"]:
                body = {"action_type": x["action_type"], "priority": x["priority"],
                        "parameters": x["parameters"]}
                if x["scope"]:
                    body["scope_reference"] = param_guid.get((x["scope"]["action"], x["scope"]["parameter"]))
                resp = r.write("POST", f"missions/{g}/actions", body)
                r.cambios -= 1          # las actions cuentan dentro de su mission
                if resp is not None:
                    for p in r.get(f"missions/{g}/actions/{resp['guid']}")["parameters"]:
                        param_guid[(x["priority"], p["id"])] = p["guid"]
            if a.escribir and robot_actions(r, g) != m["actions"]:
                print("  ¡OJO! releída no coincide con el catálogo")
        except MirApiError as e:
            print(f"  RECHAZADA {e.status}: {json.dumps(e.body, ensure_ascii=False)}")
    r.fin()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("positions")
    p.add_argument("serial")
    p.add_argument("guid_mapa")
    p.add_argument("--escribir", action="store_true")
    p.set_defaults(func=deploy_positions)
    m = sub.add_parser("missions")
    m.add_argument("serial")
    m.add_argument("guid_grupo")
    m.add_argument("--escribir", action="store_true")
    m.set_defaults(func=deploy_missions)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
