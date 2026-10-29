"""Captura recursos de un MiR al catálogo (`catalog/`), con GUID estándar.

Uso:
    catalog_capture.py positions <serial> <mapa_origen> <guid_mapa> [--escribir]
    catalog_capture.py missions  <serial> <grupo_origen> <guid_grupo> <nombre_grupo>
                                 <guid_session> [--escribir]

Ejemplos (fase 0 de docs/plan-admin.md):
    catalog_capture.py positions mir-2 suberri imperialfleet-map0000000-01-0001-001
    catalog_capture.py missions mir-2 mirur-tknika imperialfleet-missiongrp-01-0000-001 \\
        mirur-tknikav2 imperialfleet-session000-01-0000-001

Sin `--escribir` solo enseña lo que haría. Solo hace GET al robot.

- positions: las de <mapa_origen>, colgando del mapa del catálogo. Se
  numeran dentro de su mapa, en el orden del robot (= orden de creación).
- missions: las del grupo <grupo_origen> **y todo lo que llaman**
  (sub-missions de otros grupos, sonidos subidos a mano), en el grupo del
  catálogo <guid_grupo>. Las positions que usen tienen que estar ya en el
  catálogo (capturadas de este mismo robot). Volver a capturar conserva los
  GUIDs ya asignados (se reconocen por `_origen`).

Formato del GUID: docs/plan-admin.md §4. Qué lleva GUID nuestro y qué no:
decisiones 77 y 78 de docs/avance.md.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import date
from pathlib import Path

from _common import CATALOG, client_for, load, robot_or_exit

from fm.adapters.mir.sync import AUTO_CHILD_TYPES, FACTORY, GUIDISH, parse_std, std_guid  # noqa: E402

# Cabeceras de audio → extensión del fichero del catálogo.
AUDIO_MAGIC = {b"RIFF": "wav", b"ID3": "mp3", b"\xff\xfb": "mp3", b"OggS": "ogg"}


def read_catalog(folder: str) -> list[dict]:
    return [json.loads(f.read_text(encoding="utf-8"))
            for f in sorted((CATALOG / folder).glob("*.json"))]


def write(folder: str, doc: dict, escribir: bool) -> None:
    if escribir:
        out = CATALOG / folder
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{doc['guid']}.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def origen(serial: str, guid: str, **extra) -> dict:
    return {"robot": serial, **extra, "guid": guid, "capturado": date.today().isoformat()}


# ------------------------------------------------------------------ positions
def capture_positions(a: argparse.Namespace) -> None:
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
            "_origen": origen(a.serial, p["guid"], mapa=a.mapa_origen),
        }
        hijo = f" (hija de {doc['parent_id'][-3:]})" if doc["parent_id"] else ""
        print(f"{g}  tipo {p['type_id']:>2}  {p['name']}{hijo}"
              + ("  + docking offset" if offsets else ""))
        write("positions", doc, a.escribir)
    print(f"\n{len(positions)} positions" + ("" if a.escribir else " (prueba: añade --escribir)"))


# ------------------------------------------------------------------- missions
def capture_missions(a: argparse.Namespace) -> None:
    _, inst, _, group_num = parse_std(a.guid_grupo)
    c = client_for(robot_or_exit(load(), a.serial))
    groups = {g["name"]: g["guid"] for g in c._req("GET", "mission_groups")}
    if a.grupo_origen not in groups:
        sys.exit(f"'{a.grupo_origen}' no está en {a.serial}; grupos: {sorted(groups)}")
    src_group = groups[a.grupo_origen]

    # Lo que ya hay en el catálogo y vino de este robot: GUID del robot → nuestro.
    def known(folder: str) -> dict[str, str]:
        return {d["_origen"]["guid"]: d["guid"] for d in read_catalog(folder)
                if d.get("_origen", {}).get("robot") == a.serial}
    pos_map = known("positions")
    mission_map = known("missions")
    sound_map = known("sounds")

    def next_num(guids, padre=None) -> int:
        nums = [parse_std(g)[3] for g in guids if padre is None or parse_std(g)[2] == padre]
        return max(nums, default=0) + 1

    # 1. Missions a capturar: las del grupo y, recorriendo sus load_mission,
    #    las que llaman (aunque sean de otro grupo).
    pending = [m["guid"] for m in c._req("GET", f"mission_groups/{src_group}/missions")]
    missions: dict[str, tuple[dict, list[dict]]] = {}
    while pending:
        g = pending.pop(0)
        if g in missions:
            continue
        m = c._req("GET", f"missions/{g}")
        acts = sorted((c._req("GET", f"missions/{g}/actions/{x['guid']}")
                       for x in c._req("GET", f"missions/{g}/actions")),
                      key=lambda x: x["priority"])
        missions[g] = (m, acts)
        for x in acts:
            if x["action_type"] == "load_mission":
                pending += [p["value"] for p in x["parameters"] if p["id"] == "mission_id"]

    # 2. GUIDs nuevos (missions numeradas en toda la instalación, decisión 72).
    for g in missions:
        if g not in mission_map:
            mission_map[g] = std_guid("mission", inst, group_num,
                                      next_num(mission_map.values()))

    # 3. Traducir y escribir.
    errores: list[str] = []
    sounds_needed: set[str] = set()

    def translate(value, where: str):
        if not (isinstance(value, str) and GUIDISH.match(value)) or value.startswith(FACTORY):
            return value
        for table in (pos_map, mission_map, sound_map):
            if value in table:
                return table[value]
        if value in sound_guids:
            sounds_needed.add(value)
            return value          # se traduce al final, cuando el sonido tenga GUID
        errores.append(f"{where}: {value} no está en el catálogo")
        return value

    sound_guids = {s["guid"] for s in c._req("GET", "sounds")}
    docs = []
    for g, (m, acts) in missions.items():
        # scope_reference apunta al GUID de un PARÁMETRO de la action contenedora
        # (try/catch, if…). Como esos GUIDs los pone el MiR, se guarda como
        # (priority de la contenedora, id del parámetro) — decisión 78.
        param_owner = {p["guid"]: (x["priority"], p["id"])
                       for x in acts for p in x["parameters"]}
        actions = []
        for x in acts:
            where = f"{m['name']!r} action {x['priority']} ({x['action_type']})"
            scope = None
            if x["scope_reference"]:
                prio, pid = param_owner[x["scope_reference"]]
                scope = {"action": prio, "parameter": pid}
            actions.append({
                "priority": x["priority"],
                "action_type": x["action_type"],
                "scope": scope,
                "parameters": [{"id": p["id"], "value": translate(p["value"], where),
                                "input_name": p["input_name"]} for p in x["parameters"]],
            })
        docs.append({
            "guid": mission_map[g],
            "name": m["name"],
            "description": m["description"],
            "group_id": a.guid_grupo,
            "session_id": a.guid_session,
            "hidden": m["hidden"],
            "actions": actions,
            "_origen": origen(a.serial, g, grupo=next(
                (n for n, gg in groups.items() if gg == m["group_id"]), m["group_id"])),
        })

    # 4. Sonidos subidos a mano (los de fábrica se referencian tal cual).
    sound_docs = []
    for sg in sorted(sounds_needed):
        s = c._req("GET", f"sounds/{sg}")
        sound_map[sg] = sound_map.get(sg) or std_guid("sound", inst, 0, next_num(sound_map.values()))
        audio = base64.b64decode(s["sound"])
        ext = next((e for magic, e in AUDIO_MAGIC.items() if audio.startswith(magic)), "bin")
        sound_docs.append(({"guid": sound_map[sg], "name": s["name"], "note": s["note"],
                            "volume": s["volume"], "sound": f"{sound_map[sg]}.{ext}",
                            "_origen": origen(a.serial, sg)}, audio))
    for d in docs:
        for x in d["actions"]:
            for p in x["parameters"]:
                p["value"] = sound_map.get(p["value"], p["value"]) if isinstance(p["value"], str) else p["value"]

    group_doc = {"guid": a.guid_grupo, "name": a.nombre_grupo,
                 "_origen": origen(a.serial, src_group, grupo=a.grupo_origen)}
    print(f"grupo {a.guid_grupo}  {a.nombre_grupo!r}  (de {a.grupo_origen!r})")
    for d in docs:
        extra = "" if d["_origen"]["grupo"] == a.grupo_origen else f"  ← llamada desde el grupo, viene de {d['_origen']['grupo']!r}"
        print(f"{d['guid']}  {d['name']!r}  {len(d['actions'])} actions{extra}")
    for d, audio in sound_docs:
        print(f"{d['guid']}  sonido {d['name']!r} ({len(audio)} bytes, {d['sound']})")
    if errores:
        print("\nNO SE ESCRIBE NADA: referencias sin resolver")
        for e in errores:
            print("  -", e)
        sys.exit(1)

    write("mission_groups", group_doc, a.escribir)
    for d in docs:
        write("missions", d, a.escribir)
    for d, audio in sound_docs:
        write("sounds", d, a.escribir)
        if a.escribir:
            (CATALOG / "sounds" / d["sound"]).write_bytes(audio)
    print(f"\n{len(docs)} missions, {len(sound_docs)} sonidos"
          + ("" if a.escribir else " (prueba: añade --escribir)"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("positions")
    p.add_argument("serial")
    p.add_argument("mapa_origen", help="nombre del mapa en el robot")
    p.add_argument("guid_mapa", help="GUID estándar del mapa del catálogo al que cuelgan")
    p.add_argument("--escribir", action="store_true")
    p.set_defaults(func=capture_positions)
    m = sub.add_parser("missions")
    m.add_argument("serial")
    m.add_argument("grupo_origen", help="nombre del mission_group en el robot")
    m.add_argument("guid_grupo", help="GUID estándar del grupo en el catálogo")
    m.add_argument("nombre_grupo", help="nombre del grupo en el catálogo")
    m.add_argument("guid_session", help="GUID estándar de la session de las missions")
    m.add_argument("--escribir", action="store_true")
    m.set_defaults(func=capture_missions)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
