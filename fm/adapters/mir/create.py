"""Pestaña Crear (docs/plan-admin.md §3): dar de alta en UN MiR una mission
vacía, una position o una zona con GUID estándar (§4), para seguir en su web.

El ciclo de un recurso nuevo:

    Crear (aquí)  →  web del MiR  →  Capturar (aquí)  →  Sincronización  →  Configurador
    GUID nuestro     Blockly / mapa   al catálogo         a otros robots     habilitar action

**Adoptar** (decisión 116): una mission hecha en la web del MiR, que no
tiene GUID de la flota (p. ej. un "Guardar como" de otra), se lleva al
catálogo con un GUID estándar: el de una mission que ya está en el catálogo
(la sustituye: sus actions pasan a ser las de la adoptada) o uno nuevo de su
grupo. El MiR no deja cambiar el GUID de una mission, así que en ese robot
la adoptada sigue con el suyo: se despliega la del catálogo (Sincronización)
y la copia se borra en la web del MiR.

- **Mission:** vacía, en un grupo del catálogo. Se sigue en
  `http://<ip>/missions/<guid>` (Blockly).
- **Position:** en la posición ACTUAL del robot, sobre un mapa del
  catálogo que tiene que ser su mapa activo: quien no sabe dónde está el
  (0, 0) del mapa la encuentra donde está el robot. Se ajusta en el editor
  del mapa, `http://<ip>/maps/<guid del mapa>`. El tipo se elige al crearla
  (`POSITION_TYPES`): la web del MiR no deja cambiarlo después (decisión
  119). Un VL-marker nace con un docking offset provisional y su entrada
  la crea el MiR; en la web se vuelve a detectar el marcador y se ajusta
  el offset, y al capturar se recogen los dos.
- **Site** (decisión 128): la session del MiR, el entorno donde viven los
  mapas; es lo primero que se crea en una flota nueva. Como el mapa, solo
  se escribe en el catálogo y llega a los robots desde Sincronización.
- **Mapa** (decisión 126): se sube un PNG desde el PC, con su resolución y
  su origen, a un site del catálogo o a uno nuevo. Solo se escribe en el
  catálogo (completo, sin borrador): llega a los robots desplegándolo en
  Sincronización, como cualquier otro recurso.
- **Zona** (decisión 124): un cuadrado de 1 m delante del robot, en su mapa
  activo, con el tipo elegido (`ZONE_TYPES`; el MiR tampoco deja cambiarlo
  después). En el editor del mapa se le da la forma y, si es de velocidad o
  de sonido y luz, sus acciones. Al capturar se recogen el polígono y las
  acciones.

**Número del GUID** (§4): el máximo de los GUIDs estándar del mismo tipo (y
del mismo mapa, en las positions) que hay en el catálogo — borradores y
retirados incluidos (`catalog/retirados.json`, `remove.py`) — y en los
robots que respondan, más uno. Al crear, el recurso se
apunta en `catalog/` como **borrador** (`"borrador": true`), así que su
número queda reservado aunque el robot luego esté apagado; los borradores
no se despliegan (`Catalog.drafts`). **Capturar** lo relee del robot y
escribe el documento completo, ya sin borrador.

Solo GET y POST de alta: nada de esto mueve el robot.
"""
from __future__ import annotations

import base64
import json
import math
import struct
from datetime import date
from pathlib import Path
from typing import Iterable

from fm.adapters.mir.client import MirApiError
from fm.adapters.mir.remove import retired, usage_blockers
from fm.adapters.mir.sync import (AUTO_CHILD_TYPES, FACTORY, FOLDERS, GUIDISH, Catalog, is_std,
                                  normalized_actions, normalized_zone, parse_std, std_guid)

POSITION_NORMAL = 0
MARKER_TYPES = (11, 20)          # marcador VL y cargador: el MiR les crea una entrada (12, 21)
# Tipos que se pueden crear desde la pestaña (decisión 119): los que usa hoy
# la flota y están probados con el robot. Otro tipo = una fila más y una
# prueba real (qué exige el MiR al crearlo, si crea él la entrada…).
# `offset`: el MiR exige un docking offset al crear el marcador por la API.
POSITION_TYPES = {
    0: {"label": "Robot position", "offset": False},
    11: {"label": "VL-marker", "offset": True},
    20: {"label": "MiR Charge 48V", "offset": False},
}
# Provisional (el de los VL-marker del catálogo, ~0,8 m antes del marcador):
# se ajusta en la web al volver a detectar el marcador.
PROVISIONAL_OFFSET = {"offset_x": -0.8, "offset_y": 0.0, "offset_orientation": 0.0}
# Tipos de zona que se pueden crear (decisión 124), con el nombre que les da
# la web del MiR y un resumen de su ayuda (decisión 125). Todos probados por
# la API en mir-2 el 2026-10-29. Fuera: 207 (el MiR dice "Zone type not
# found"), 103 y 106 (son de MiR Fleet) y 105 (módulo E/S: apunta a un
# módulo propio de cada robot).
ZONE_TYPES = {
    206: {"label": "Zona prohibida",
          "help": "El robot nunca entra: ni al planificar el trayecto ni al esquivar un obstáculo."},
    205: {"label": "Zona no preferida",
          "help": "El robot la evita al planificar y solo la cruza si todos los demás caminos están "
                  "bloqueados. Útil para alejarlo de pasillos con mucha gente."},
    204: {"label": "Zona preferida",
          "help": "El robot intenta planificar su trayecto por ella; sale de la zona si tiene que "
                  "esquivar un obstáculo."},
    203: {"label": "Zona direccional",
          "help": "Dentro de ella el trayecto siempre va en el sentido de la flecha (se fija en el editor "
                  "del mapa). Las positions cercanas deben mirar en ese sentido."},
    208: {"label": "Zona de acceso",
          "help": "El robot ignora los obstáculos detectados dentro al planificar, para pasar por rampas "
                  "o puertas. Si encuentra uno, se acerca y espera a que desaparezca."},
    104: {"label": "Zona de velocidad",
          "help": "Dentro, el robot circula con otro límite de velocidad: más bajo donde hay gente, más "
                  "alto en zonas despejadas."},
    102: {"label": "Zona de sonido y de luz",
          "help": "Dentro, el robot puede emitir un sonido y hacer parpadear sus luces para avisar a "
                  "las personas."},
    101: {"label": "Zona del planificador",
          "help": "Dentro, el robot puede localizarse solo con los encoders, reducir su campo de visión, "
                  "ajustar el tiempo de espera y la desviación del trayecto o ignorar obstáculos."},
}
ZONE_FORBIDDEN = 206
# Cuadrado provisional: 1 m de lado, con su centro 2 m delante del robot. No
# encima: una zona prohibida bajo el robot le impediría planificar.
ZONE_SIDE, ZONE_AHEAD = 1.0, 2.0
OFFSET_KEYS = ("docking_type", "name", "x_offset", "y_offset", "orientation_offset",
               "bar_length", "bar_distance", "shelf_leg_asymmetry_x")


class CreateError(ValueError):
    """No se puede crear o capturar (nombre repetido, recurso que falta en el
    robot, referencia que no está en el catálogo...). El mensaje va a la UI."""


def next_guid(kind: str, inst: int, parent: int, taken: Iterable[str], per_parent: bool) -> str:
    """Siguiente GUID estándar libre de `kind` (§4)."""
    nums = [parse_std(g)[3] for g in taken
            if is_std(g) and parse_std(g)[0] == kind and (not per_parent or parse_std(g)[2] == parent)]
    n = max(nums, default=0) + 1
    if n > 999:
        raise CreateError(f"no quedan números para {kind} (máximo 999)")
    return std_guid(kind, inst, parent, n)


def robot_std_guids(client, kind: str, map_guid: str | None = None) -> set[str]:
    """GUIDs estándar que ya hay en un robot (para no repetir número)."""
    if kind in ("mission", "map", "session"):        # se numeran en toda la instalación
        rows = client._req("GET", FOLDERS[kind])
    else:                              # position o zona: van por mapa
        rows = client._req("GET", f"maps/{map_guid}/{FOLDERS[kind]}")
    return {r["guid"] for r in rows if is_std(r["guid"])}


def write_doc(catalog_dir: Path, kind: str, doc: dict) -> None:
    out = Path(catalog_dir) / FOLDERS[kind]
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{doc['guid']}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                                             encoding="utf-8")


def _origen(serial: str, guid: str, **extra) -> dict:
    return {"robot": serial, **extra, "guid": guid, "capturado": date.today().isoformat()}


def _retired(sync) -> set[str]:
    """Números retirados de la flota: no se reutilizan (decisión 105)."""
    return {r["guid"] for r in retired(sync.catalog_dir)}


def _get_or(client, path: str, msg: str):
    try:
        return client._req("GET", path)
    except MirApiError as e:
        if e.status == 404:
            raise CreateError(msg) from None
        raise


# ------------------------------------------------------------------- crear
def create_mission(sync, name: str, group_guid: str, others: Iterable[str] = ()) -> dict:
    """Mission vacía `name` en el grupo `group_guid` del catálogo, en el robot
    de `sync`. `others`: GUIDs estándar de los demás robots."""
    name = (name or "").strip()
    if not name:
        raise CreateError("falta el nombre")
    cat = sync.catalog()
    group = cat.items.get(group_guid)
    if group is None or cat.kind(group_guid) != "missiongrp":
        raise CreateError(f"el grupo {group_guid} no está en el catálogo")
    every = {**cat.items, **cat.drafts}
    same_group = [d for g, d in every.items() if cat.kind(g) == "mission" and d.get("group_id") == group_guid]
    sessions = [d["session_id"] for d in same_group if d.get("session_id")]
    if not sessions:
        raise CreateError(f"no se sabe la session del grupo {group['name']!r}: no hay missions suyas en el catálogo")
    session = max(set(sessions), key=sessions.count)
    _, inst, _, group_num = parse_std(group_guid)
    with sync.lock:
        c = sync.client
        _get_or(c, f"mission_groups/{group_guid}",
                f"el grupo {group['name']!r} no está en {sync.serial}: despliégalo antes en Sincronización")
        names = {m["name"] for m in c._req("GET", f"mission_groups/{group_guid}/missions")}
        names |= {d["name"] for d in same_group}
        if name in names:
            raise CreateError(f"ya hay una mission {name!r} en el grupo {group['name']!r}")
        taken = set(every) | _retired(sync) | robot_std_guids(c, "mission") | set(others)
        guid = next_guid("mission", inst, group_num, taken, per_parent=False)
        doc = {"guid": guid, "name": name, "description": "", "group_id": group_guid,
               "session_id": session, "hidden": False}
        c._req("POST", "missions", json=doc)
    write_doc(sync.catalog_dir, "mission",
              {**doc, "actions": [], "borrador": True,
               "_origen": _origen(sync.serial, guid, grupo=group["name"])})
    return {"kind": "mission", "guid": guid, "name": name, "url": sync.web_url(guid)}


def create_position(sync, name: str, map_guid: str, others: Iterable[str] = (),
                    type_id: int = POSITION_NORMAL) -> dict:
    """Position `name` de tipo `type_id` (`POSITION_TYPES`) en el mapa
    `map_guid` del catálogo, donde está ahora el robot de `sync` (ese mapa
    tiene que ser su mapa activo)."""
    name = (name or "").strip()
    if not name:
        raise CreateError("falta el nombre")
    kind = POSITION_TYPES.get(type_id)
    if kind is None:
        raise CreateError(f"tipo de position {type_id} no admitido aquí: créala en la web del MiR")
    cat = sync.catalog()
    mp = cat.items.get(map_guid)
    if mp is None or cat.kind(map_guid) != "map":
        raise CreateError(f"el mapa {map_guid} no está en el catálogo")
    every = {**cat.items, **cat.drafts}
    same_map = [d for g, d in every.items() if cat.kind(g) == "position" and d.get("map_id") == map_guid]
    _, inst, _, map_num = parse_std(map_guid)
    with sync.lock:
        c = sync.client
        _get_or(c, f"maps/{map_guid}",
                f"el mapa {mp['name']!r} no está en {sync.serial}: despliégalo antes en Sincronización")
        st = c._req("GET", "status")
        if st.get("map_id") != map_guid:
            raise CreateError(f"{sync.serial} no está trabajando en el mapa {mp['name']!r}: su posición actual "
                              "no vale para ese mapa")
        names = {p["name"] for p in c._req("GET", f"maps/{map_guid}/positions")}
        names |= {d["name"] for d in same_map}
        if name in names:
            raise CreateError(f"ya hay una position {name!r} en el mapa {mp['name']!r}")
        taken = set(every) | _retired(sync) | robot_std_guids(c, "position", map_guid) | set(others)
        guid = next_guid("position", inst, map_num, taken, per_parent=True)
        pos = st["position"]
        doc = {"guid": guid, "name": name, "type_id": type_id,
               "pos_x": round(pos["x"], 3), "pos_y": round(pos["y"], 3),
               "orientation": round(pos["orientation"], 2), "map_id": map_guid}
        c._req("POST", "positions", json={**doc, **(PROVISIONAL_OFFSET if kind["offset"] else {})})
    # Borrador: la entrada que crea el MiR y el offset de verdad entran al capturar.
    write_doc(sync.catalog_dir, "position",
              {**doc, "parent_id": None, "docking_offsets": [], "creada_por_mir": False, "borrador": True,
               "_origen": _origen(sync.serial, guid, mapa=mp["name"])})
    return {"kind": "position", "guid": guid, "name": name, "url": sync.web_url(guid, map_guid),
            "pose": [doc["pos_x"], doc["pos_y"], doc["orientation"]], "type": kind["label"],
            "marker": type_id in MARKER_TYPES}


def placeholder_square(x: float, y: float, orientation_deg: float) -> list[dict]:
    """Vértices del cuadrado provisional de una zona nueva, delante del robot."""
    a = math.radians(orientation_deg)
    cx, cy = x + ZONE_AHEAD * math.cos(a), y + ZONE_AHEAD * math.sin(a)
    h = ZONE_SIDE / 2
    return [{"x": round(cx + dx, 3), "y": round(cy + dy, 3)} for dx, dy in ((-h, -h), (h, -h), (h, h), (-h, h))]


def create_zone(sync, name: str, map_guid: str, others: Iterable[str] = (),
                type_id: int = ZONE_FORBIDDEN) -> dict:
    """Zona `name` de tipo `type_id` (`ZONE_TYPES`) en el mapa `map_guid` del
    catálogo: un cuadrado delante del robot de `sync`, que tiene que estar
    trabajando en ese mapa (es su posición la que dice dónde ponerla)."""
    name = (name or "").strip()
    if not name:
        raise CreateError("falta el nombre")
    if type_id not in ZONE_TYPES:
        raise CreateError(f"tipo de zona {type_id} no admitido aquí: créala en la web del MiR")
    cat = sync.catalog()
    mp = cat.items.get(map_guid)
    if mp is None or cat.kind(map_guid) != "map":
        raise CreateError(f"el mapa {map_guid} no está en el catálogo")
    every = {**cat.items, **cat.drafts}
    same_map = [d for g, d in every.items() if cat.kind(g) == "zone" and d.get("map_id") == map_guid]
    _, inst, _, map_num = parse_std(map_guid)
    with sync.lock:
        c = sync.client
        _get_or(c, f"maps/{map_guid}",
                f"el mapa {mp['name']!r} no está en {sync.serial}: despliégalo antes en Sincronización")
        st = c._req("GET", "status")
        if st.get("map_id") != map_guid:
            raise CreateError(f"{sync.serial} no está trabajando en el mapa {mp['name']!r}: su posición actual "
                              "no vale para ese mapa")
        names = {z["name"] for z in c._req("GET", f"maps/{map_guid}/zones")}
        names |= {d["name"] for d in same_map}
        if name in names:
            raise CreateError(f"ya hay una zona {name!r} en el mapa {mp['name']!r}")
        taken = set(every) | _retired(sync) | robot_std_guids(c, "zone", map_guid) | set(others)
        guid = next_guid("zone", inst, map_num, taken, per_parent=True)
        pos = st["position"]
        body = {"guid": guid, "name": name, "type_id": type_id, "map_id": map_guid,
                "polygon": placeholder_square(pos["x"], pos["y"], pos["orientation"]), "actions": []}
        c._req("POST", "zones", json=body)
        doc = normalized_zone(c._req("GET", f"zones/{guid}"))
    # Borrador: la forma y las acciones de verdad entran al capturar.
    write_doc(sync.catalog_dir, "zone",
              {"guid": guid, **doc, "borrador": True, "_origen": _origen(sync.serial, guid, mapa=mp["name"])})
    return {"kind": "zone", "guid": guid, "name": name, "url": sync.web_url(guid, map_guid),
            "type": ZONE_TYPES[type_id]["label"]}


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(data: bytes) -> tuple[int, int]:
    """(ancho, alto) de un PNG en gris de 8 bits, el formato en que el MiR
    guarda `base_map` (0 = obstáculo, 255 = libre). El MiR lo guarda tal
    cual, sin convertir (decisión 76), así que otro formato se rechaza: la
    web del MiR exporta así sus mapas."""
    if data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise CreateError("el fichero no es un PNG")
    width, height, depth, color = struct.unpack(">IIBB", data[16:26])
    if (depth, color) != (8, 0):
        raise CreateError(f"el PNG tiene que estar en gris de 8 bits (este es de {depth} bits y "
                          f"{'color' if color in (2, 3, 6) else 'gris con transparencia' if color == 4 else 'otro tipo'}): "
                          "expórtalo así desde un editor de imágenes o desde la web del MiR")
    return width, height


def create_session(sync, name: str, description: str = "", others: Iterable[str] = ()) -> dict:
    """Site `name` (session del MiR) en el catálogo. No escribe en ningún
    robot. `others`: GUIDs estándar de sites de los robots."""
    name = (name or "").strip()
    if not name:
        raise CreateError("falta el nombre del site")
    cat = sync.catalog()
    every = {**cat.items, **cat.drafts}
    if any(cat.kind(g) == "session" and d["name"] == name for g, d in every.items()):
        raise CreateError(f"ya hay un site {name!r} en el catálogo")
    inst = parse_std(next(iter(every)))[1] if every else 1
    guid = next_guid("session", inst, 0, set(every) | _retired(sync) | set(others), per_parent=False)
    doc = {"guid": guid, "name": name, "description": (description or "").strip(),
           "_origen": {"guid": guid, "capturado": date.today().isoformat()}}
    write_doc(sync.catalog_dir, "session", doc)
    return {"kind": "session", "guid": guid, "name": name}


def create_map(sync, name: str, png: bytes, resolution: float, origin_x: float, origin_y: float,
               session_guid: str | None = None, new_session: str = "", others: Iterable[str] = (),
               filename: str = "") -> dict:
    """Mapa `name` con la imagen `png` en el site `session_guid` del catálogo
    o, si no se da, en un site nuevo `new_session` (`create_session`). Solo
    escribe en el catálogo. `others`: GUIDs estándar de mapas y sites de los
    robots."""
    name, new_session = (name or "").strip(), (new_session or "").strip()
    if not name:
        raise CreateError("falta el nombre del mapa")
    if not resolution or resolution <= 0:
        raise CreateError("la resolución tiene que ser mayor que 0 (metros por píxel)")
    width, height = png_size(png)
    cat = sync.catalog()
    every = {**cat.items, **cat.drafts}
    if any(cat.kind(g) == "map" and d["name"] == name for g, d in every.items()):
        raise CreateError(f"ya hay un mapa {name!r} en el catálogo")
    if session_guid:
        if cat.kind(session_guid) != "session" or session_guid not in cat.items:
            raise CreateError(f"el site {session_guid} no está en el catálogo")
        session, new_site = cat.items[session_guid], False
    else:
        if not new_session:
            raise CreateError("elige un site (o crea uno antes)")
        created = create_session(sync, new_session, others=others)
        cat = sync.catalog()
        session, new_site = cat.items[created["guid"]], True
    every = {**cat.items, **cat.drafts}
    _, inst, _, session_num = parse_std(session["guid"])
    guid = next_guid("map", inst, session_num, set(every) | _retired(sync) | set(others), per_parent=False)
    doc = {"guid": guid, "name": name, "session_id": session["guid"], "resolution": float(resolution),
           "origin_x": float(origin_x), "origin_y": float(origin_y), "origin_theta": 0.0,
           "base_map": f"{guid}.png",
           "_origen": {"fichero": filename or "", "guid": guid, "capturado": date.today().isoformat()}}
    out = Path(sync.catalog_dir) / FOLDERS["map"]
    out.mkdir(parents=True, exist_ok=True)
    (out / doc["base_map"]).write_bytes(png)
    write_doc(sync.catalog_dir, "map", doc)
    return {"kind": "map", "guid": guid, "name": name, "site": session["name"],
            "new_site": new_site, "size": [width, height],
            "meters": [round(width * resolution, 2), round(height * resolution, 2)]}


def png_from_b64(data: str) -> bytes:
    """El PNG que manda la web (base64, con o sin prefijo `data:`)."""
    try:
        return base64.b64decode(data.split(",", 1)[1] if data.startswith("data:") else data, validate=True)
    except (ValueError, IndexError):
        raise CreateError("la imagen no ha llegado bien (base64 no válido)") from None


# ----------------------------------------------------------------- capturar
def capture(sync, guid: str) -> dict:
    """Relee del robot un recurso creado aquí (borrador, o ya capturado de
    este mismo robot) y escribe su documento completo en el catálogo."""
    cat = sync.catalog()
    old = cat.drafts.get(guid) or cat.items.get(guid)
    if old is None:
        raise CreateError(f"{guid} no está en el catálogo")
    if old.get("_origen", {}).get("robot") != sync.serial:
        raise CreateError(f"{old['name']!r} se creó en {old.get('_origen', {}).get('robot')}: captúralo desde ese robot")
    kind = cat.kind(guid)
    with sync.lock:
        if kind == "mission":
            docs = [_capture_mission(sync, cat, old)]
        elif kind == "position":
            docs = _capture_position(sync, cat, old)
        elif kind == "zone":
            docs = [_capture_zone(sync, cat, old)]
        else:
            raise CreateError(f"no se captura un recurso de tipo {kind} desde aquí")
    for k, d in docs:
        write_doc(sync.catalog_dir, k, d)
    main = docs[0][1]
    if kind == "mission":
        detail = f"{len(main['actions'])} actions"
    elif kind == "zone":
        detail = (f"{ZONE_TYPES[main['type_id']]['label'] if main['type_id'] in ZONE_TYPES else main['type_id']}, "
                  f"{len(main['polygon'])} vértices"
                  + (f", {len(main['actions'])} acciones" if main["actions"] else ""))
    else:
        detail = f"tipo {main['type_id']}" + (" + entrada" if len(docs) > 1 else "")
    return {"kind": kind, "guid": guid, "name": main["name"], "detail": detail}


def _capture_mission(sync, cat: Catalog, old: dict, source: str | None = None) -> tuple[str, dict]:
    """Documento del catálogo para `old` con lo que hay en el robot. Se lee
    la mission `source` (adoptar) o, por defecto, la del mismo GUID."""
    c, guid = sync.client, source or old["guid"]
    m = _get_or(c, f"missions/{guid}", f"{old['name'] if not source else guid!r} ya no está en {sync.serial}")
    # GUID del robot → del catálogo, para lo capturado antes de este mismo robot
    # (positions del mapa antiguo, sub-missions...).
    known = {d["_origen"]["guid"]: g for g, d in cat.items.items()
             if d.get("_origen", {}).get("robot") == sync.serial and d["_origen"].get("guid")}
    errors = []
    actions = normalized_actions(c, guid)
    for a in actions:
        for p in a["parameters"]:
            v = p["value"]
            if not isinstance(v, str) or len(v) != 36 or v.startswith(FACTORY):
                continue
            if is_std(v):
                if v in cat.drafts:
                    errors.append(f"usa {cat.drafts[v]['name']!r}, que aún es un borrador: captúralo antes")
                elif v not in cat.items:
                    errors.append(f"usa {v}, que no está en el catálogo")
            elif v in known:
                p["value"] = known[v]
            elif GUIDISH.match(v):            # GUID propio del robot
                errors.append(f"action {a['priority']} ({a['action_type']}) usa {v}, que no está en el "
                              "catálogo (créalo desde aquí o captúralo con scripts/catalog_capture.py)")
    if errors:
        raise CreateError(f"no se captura {old['name']!r}: " + "; ".join(errors))
    group = m["group_id"] if m["group_id"] in cat.items else old["group_id"]
    doc = {k: v for k, v in old.items() if k != "borrador"}
    doc.update(name=m["name"], description=m.get("description", ""), hidden=m.get("hidden", False),
               group_id=group, actions=actions,
               _origen=_origen(sync.serial, guid, grupo=cat.items.get(group, {}).get("name", group)))
    return "mission", doc


def _capture_position(sync, cat: Catalog, old: dict) -> list[tuple[str, dict]]:
    c, guid = sync.client, old["guid"]
    p = _get_or(c, f"positions/{guid}", f"{old['name']!r} ya no está en {sync.serial}")
    offsets = [c._req("GET", f"docking_offsets/{o['guid']}")
               for o in c._req("GET", f"positions/{guid}/docking_offsets")]
    doc = {k: v for k, v in old.items() if k != "borrador"}
    doc.update({k: p[k] for k in ("name", "type_id", "pos_x", "pos_y", "orientation")},
               docking_offsets=[{k: o.get(k) for k in OFFSET_KEYS} for o in offsets[:1]],
               _origen=_origen(sync.serial, guid, mapa=old.get("_origen", {}).get("mapa", "")))
    docs = [("position", doc)]
    if p["type_id"] in MARKER_TYPES:
        # Se ha convertido en marcador/cargador en la web: el MiR le ha creado
        # una entrada con GUID propio. En el catálogo lleva GUID estándar solo
        # para nombrar su fichero (decisión 77).
        hijas = c._req("GET", f"positions/{guid}/helper_positions")
        if hijas:
            e = c._req("GET", f"positions/{hijas[0]['guid']}")
            prev = next((g for g, d in cat.items.items() if d.get("parent_id") == guid), None)
            _, inst, _, map_num = parse_std(old["map_id"])
            eg = prev or next_guid("position", inst, map_num, {*cat.items, *cat.drafts, *_retired(sync)},
                                   per_parent=True)
            docs.append(("position", {
                "guid": eg, "name": e["name"], "type_id": e["type_id"],
                **{k: e[k] for k in ("pos_x", "pos_y", "orientation")},
                "map_id": old["map_id"], "parent_id": guid, "docking_offsets": [],
                "creada_por_mir": e["type_id"] in AUTO_CHILD_TYPES,
                "_origen": _origen(sync.serial, e["guid"], mapa=doc["_origen"]["mapa"])}))
    return docs


def _capture_zone(sync, cat: Catalog, old: dict) -> tuple[str, dict]:
    """La zona tal como la ha dejado la web del MiR. Sus acciones solo pueden
    apuntar a recursos de fábrica o del catálogo (p. ej. el sonido de una
    zona de sonido): un GUID propio del robot no existiría en los demás."""
    c, guid = sync.client, old["guid"]
    z = normalized_zone(_get_or(c, f"zones/{guid}", f"{old['name']!r} ya no está en {sync.serial}"))
    errors = []
    for a in z["actions"]:
        for p in a["parameters"]:
            v = p["value"]
            if not isinstance(v, str) or v.startswith(FACTORY):
                continue
            if is_std(v) and v in cat.drafts:
                errors.append(f"usa {cat.drafts[v]['name']!r}, que aún es un borrador: captúralo antes")
            elif is_std(v) and v not in cat.items:
                errors.append(f"usa {v}, que no está en el catálogo")
            elif not is_std(v) and GUIDISH.match(v):
                errors.append(f"la acción {a['type_id']} usa {v}, que no está en el catálogo "
                              "(¿un sonido subido solo a este robot?)")
    if errors:
        raise CreateError(f"no se captura {old['name']!r}: " + "; ".join(errors))
    doc = {"guid": guid, **z,
           "_origen": _origen(sync.serial, guid, mapa=old.get("_origen", {}).get("mapa", ""))}
    return "zone", doc


# ------------------------------------------------------------------ adoptar
def adoptable(sync) -> list[dict]:
    """Missions del robot de `sync` sin GUID de la flota que están en un grupo
    del catálogo: hechas (o guardadas como) en la web del MiR. Cada una con
    la mission del catálogo del mismo nombre y grupo como sugerencia, y
    `adopted_as` si ya se adoptó (falta borrar la copia en la web del MiR)."""
    cat = sync.catalog()
    groups = {g: d for g, d in cat.items.items() if cat.kind(g) == "missiongrp"}
    adopted = {d["_origen"]["guid"]: g for g, d in cat.items.items()
               if cat.kind(g) == "mission" and d.get("_origen", {}).get("robot") == sync.serial
               and d["_origen"].get("guid") and not is_std(d["_origen"]["guid"])}
    out = []
    with sync.lock:
        for gg, gd in sorted(groups.items()):
            try:
                rows = sync.client._req("GET", f"mission_groups/{gg}/missions")
            except MirApiError as e:
                if e.status == 404:          # el robot no tiene ese grupo
                    continue
                raise
            for m in rows:
                if is_std(m["guid"]):
                    continue
                same = [g for g, d in cat.items.items()
                        if cat.kind(g) == "mission" and d["name"] == m["name"] and d.get("group_id") == gg]
                out.append({"guid": m["guid"], "name": m["name"], "group": gd["name"], "group_guid": gg,
                            "suggest": same[0] if len(same) == 1 else None,
                            "adopted_as": adopted.get(m["guid"]),
                            "url": sync.web_url(m["guid"])})
    return out


def adopt(sync, source: str, target: str | None = None, others: Iterable[str] = ()) -> dict:
    """Lleva al catálogo la mission `source` del robot (sin GUID de la flota)
    con el GUID `target` de una mission que ya está en el catálogo o, si no
    se da, con uno nuevo de su grupo. `others`: GUIDs estándar de los demás
    robots (para no repetir número). No escribe nada en el robot."""
    if is_std(source):
        raise CreateError("ya tiene GUID de la flota: se captura con «Capturar»")
    cat = sync.catalog()
    with sync.lock:
        c = sync.client
        m = _get_or(c, f"missions/{source}", f"la mission {source} no está en {sync.serial}")
        if target:
            old = cat.items.get(target)
            if old is None or cat.kind(target) != "mission":
                raise CreateError(f"{target} no es una mission del catálogo")
        else:
            group = cat.items.get(m["group_id"])
            if group is None or cat.kind(m["group_id"]) != "missiongrp":
                raise CreateError(f"{m['name']!r} está en un grupo que no es del catálogo: "
                                  "muévela en la web del MiR a uno que sí lo sea")
            every = {**cat.items, **cat.drafts}
            sessions = [d["session_id"] for g, d in every.items()
                        if cat.kind(g) == "mission" and d.get("group_id") == m["group_id"] and d.get("session_id")]
            if not sessions:
                raise CreateError(f"no se sabe la session del grupo {group['name']!r}")
            _, inst, _, group_num = parse_std(m["group_id"])
            taken = set(every) | _retired(sync) | robot_std_guids(c, "mission") | set(others)
            guid = next_guid("mission", inst, group_num, taken, per_parent=False)
            old = {"guid": guid, "name": m["name"], "description": "", "group_id": m["group_id"],
                   "session_id": max(set(sessions), key=sessions.count), "hidden": False}
        kind, doc = _capture_mission(sync, cat, old, source=source)
    write_doc(sync.catalog_dir, kind, doc)
    return {"kind": kind, "guid": doc["guid"], "name": doc["name"], "source": source,
            "replaced": bool(target), "detail": f"{len(doc['actions'])} actions"}


# ------------------------------------------------------------------- borrar
def created_here(cat: Catalog, guid: str) -> dict | None:
    """El documento, si es una mission, position o zona creada desde la
    pestaña Crear (borrador o ya capturada). Se reconoce porque su GUID en el robot
    es el nuestro: lo capturado con el script trae el GUID propio del robot."""
    d = cat.drafts.get(guid) or cat.items.get(guid)
    if d is None or not is_std(guid) or cat.kind(guid) not in ("mission", "position", "zone", "map", "session"):
        return None
    return d if d.get("_origen", {}).get("guid") == guid else None


def delete_blockers(sync, guid: str, definitions: dict, wanted: Iterable[str]) -> list[str]:
    """Por qué no se puede borrar `guid` (vacío = se puede): solo lo creado
    aquí y que nadie use (`remove.usage_blockers`)."""
    cat = sync.catalog()
    if created_here(cat, guid) is None:
        return ["solo se borra desde aquí lo creado en la pestaña Crear"]
    return usage_blockers(sync, cat, guid, definitions, wanted)
