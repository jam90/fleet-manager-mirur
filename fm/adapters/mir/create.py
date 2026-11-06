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
  `http://<ip>/missions/<guid>` (Blockly). Si el grupo aún no está en ese
  robot, se crea en él (decisión 134). La session de la mission es la de
  las demás de su grupo o, en un grupo sin missions, el site de la flota.
- **Grupo de missions** (decisión 134): solo un nombre, común a toda la
  flota (no es de ningún site). Como el site, se escribe solo en el
  catálogo; llega a un robot al crear en él su primera mission o desde
  Sincronización.
- **Position:** en la posición ACTUAL del robot, sobre un mapa del
  catálogo que tiene que ser su mapa activo: quien no sabe dónde está el
  (0, 0) del mapa la encuentra donde está el robot. Se ajusta en el editor
  del mapa, `http://<ip>/maps/<guid del mapa>`. El tipo se elige al crearla
  (`POSITION_TYPES`): la web del MiR no deja cambiarlo después (decisión
  119). Un VL-marker nace con un docking offset provisional y su entrada
  la crea el MiR; en la web se vuelve a detectar el marcador y se ajusta
  el offset, y al capturar se recogen los dos.
- **Capturar un mapa** de un robot (decisión 132): un mapa hecho en su web
  (sin GUID de la flota), con su imagen, sus positions y sus zonas, entra
  en el catálogo con GUIDs de la flota. No se escribe en el robot: como al
  adoptar una mission, el MiR no deja cambiar el GUID, así que la copia del
  catálogo llega por Sincronización y se activa en su web. Volver a
  capturar el mismo mapa actualiza la copia y conserva sus GUIDs.
- **Copiar un mapa** del catálogo (#33): otro mapa con la misma imagen y el
  mismo origen, y copia de sus positions (con un prefijo o un sufijo en el
  nombre, decisión 174) y de sus zonas, con GUIDs nuevos. Solo el
  catálogo; las missions no se copian.
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
from typing import Iterable, Mapping

from fm.adapters.mir.client import MirApiError
from fm.adapters.mir.remove import retired, usage_blockers
from fm.adapters.mir.sync import (AUTO_CHILD_TYPES, FACTORY, FOLDERS, GUIDISH, Catalog, is_std,
                                  mission_group_body, normalized_actions, normalized_zone, parse_std,
                                  std_guid)

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
    if kind in ("mission", "map", "session", "missiongrp"):     # se numeran en toda la instalación
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
def group_session(cat: Catalog, group_guid: str, site: str | None) -> str:
    """Session para una mission nueva del grupo: la de las que ya tiene (la
    más repetida) o, si aún no tiene ninguna, el site de la flota `site`."""
    every = {**cat.items, **cat.drafts}
    sessions = [d["session_id"] for g, d in every.items()
                if cat.kind(g) == "mission" and d.get("group_id") == group_guid and d.get("session_id")]
    if sessions:
        return max(set(sessions), key=sessions.count)
    if site and cat.kind(site) == "session" and site in cat.items:
        return site
    raise CreateError(f"el grupo {cat.items[group_guid]['name']!r} aún no tiene missions y no hay site de la "
                      "flota: elígelo en la cabecera («Site») para saber en qué site va la mission")


def create_mission(sync, name: str, group_guid: str, others: Iterable[str] = (),
                   site: str | None = None) -> dict:
    """Mission vacía `name` en el grupo `group_guid` del catálogo, en el robot
    de `sync`. `others`: GUIDs estándar de los demás robots; `site`: el de la
    flota, para la primera mission de un grupo."""
    name = (name or "").strip()
    if not name:
        raise CreateError("falta el nombre")
    cat = sync.catalog()
    group = cat.items.get(group_guid)
    if group is None or cat.kind(group_guid) != "missiongrp":
        raise CreateError(f"el grupo {group_guid} no está en el catálogo")
    every = {**cat.items, **cat.drafts}
    same_group = [d for g, d in every.items() if cat.kind(g) == "mission" and d.get("group_id") == group_guid]
    session = group_session(cat, group_guid, site)
    _, inst, _, group_num = parse_std(group_guid)
    with sync.lock:
        c = sync.client
        try:
            c._req("GET", f"mission_groups/{group_guid}")
        except MirApiError as e:
            if e.status != 404:
                raise
            # Un grupo es solo un nombre: no hace falta pasar por Sincronización.
            c._req("POST", "mission_groups", json=mission_group_body(group))
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


def fold_name(name: str) -> str:
    """Nombre para comparar: sin mayúsculas ni espacios de más. En la lista
    de la web del MiR, «Montaje» y «montaje » se confunden igual."""
    return " ".join(str(name).split()).casefold()


def position_names(client) -> dict[str, tuple[str, str]]:
    """Positions del robot en TODOS sus mapas: nombre plegado → (nombre tal
    cual, nombre del mapa). Para no repetir nombres (decisión 174)."""
    maps = {m["guid"]: m["name"] for m in client._req("GET", "maps")}
    out: dict[str, tuple[str, str]] = {}
    for p in client._req("GET", "positions"):
        map_guid = p.get("map_id") or str(p.get("map") or "").rstrip("/").split("/")[-1]   # ".../maps/<guid>"
        out.setdefault(fold_name(p["name"]), (p["name"], maps.get(map_guid, map_guid or "?")))
    return out


def create_position(sync, name: str, map_guid: str, others: Iterable[str] = (),
                    type_id: int = POSITION_NORMAL,
                    other_names: Mapping[str, tuple[str, str]] | None = None) -> dict:
    """Position `name` de tipo `type_id` (`POSITION_TYPES`) en el mapa
    `map_guid` del catálogo, donde está ahora el robot de `sync` (ese mapa
    tiene que ser su mapa activo).

    El nombre no puede repetir el de ninguna position del catálogo ni de
    ningún mapa de este robot, ni de los demás (`other_names`: nombre
    plegado → (nombre, dónde), lo junta el servidor) (decisión 174). El
    editor de missions de la web del MiR lista las positions de todos los
    mapas sin decir de cuál es cada una: con dos «Area_calidad», es fácil
    elegir la de otro mapa sin darse cuenta."""
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
    _, inst, _, map_num = parse_std(map_guid)
    with sync.lock:
        c = sync.client
        _get_or(c, f"maps/{map_guid}",
                f"el mapa {mp['name']!r} no está en {sync.serial}: despliégalo antes en Sincronización")
        st = c._req("GET", "status")
        if st.get("map_id") != map_guid:
            raise CreateError(f"{sync.serial} no está trabajando en el mapa {mp['name']!r}: su posición actual "
                              "no vale para ese mapa")
        _check_name_free(name, every, cat, {fold: (n, f"{sync.serial} (mapa {m!r})")
                                            for fold, (n, m) in position_names(c).items()}, other_names or {})
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


def _name_in_use(name: str, every: Mapping[str, dict], cat, robot: Mapping[str, tuple[str, str]],
                 others: Mapping[str, tuple[str, str]]) -> tuple[str, str] | None:
    """(nombre tal cual, dónde) de la position del catálogo (con los
    borradores), de este robot o de los demás que ya se llama `name`, en
    cualquier mapa; None si está libre."""
    key = fold_name(name)
    for g, d in every.items():
        if cat.kind(g) == "position" and fold_name(d.get("name", "")) == key:
            mapa = (cat.items.get(d.get("map_id")) or cat.drafts.get(d.get("map_id")) or {}).get("name", "?")
            return d["name"], f"el catálogo (mapa {mapa!r})"
    return robot.get(key) or others.get(key)


def _check_name_free(name: str, every: Mapping[str, dict], cat, robot: Mapping[str, tuple[str, str]],
                     others: Mapping[str, tuple[str, str]]) -> None:
    """Lanza `CreateError` si `name` ya es el de una position del catálogo
    (con los borradores), de este robot o de los demás, en cualquier mapa."""
    where = _name_in_use(name, every, cat, robot, others)
    if where:
        existing, place = where
        same = "" if existing == name else f" ({existing!r})"
        raise CreateError(f"ya hay una position con ese nombre{same} en {place}. El editor de missions de la web "
                          "del MiR lista las de todos los mapas sin decir de cuál es: elige otro nombre")


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


def create_group(sync, name: str, others: Iterable[str] = ()) -> dict:
    """Grupo de missions `name` en el catálogo. No escribe en ningún robot.
    `others`: GUIDs estándar de grupos de los robots."""
    name = (name or "").strip()
    if not name:
        raise CreateError("falta el nombre del grupo")
    cat = sync.catalog()
    every = {**cat.items, **cat.drafts}
    if any(cat.kind(g) == "missiongrp" and d["name"] == name for g, d in every.items()):
        raise CreateError(f"ya hay un grupo {name!r} en el catálogo")
    inst = parse_std(next(iter(every)))[1] if every else 1
    guid = next_guid("missiongrp", inst, 0, set(every) | _retired(sync) | set(others), per_parent=False)
    doc = {"guid": guid, "name": name, "_origen": {"guid": guid, "capturado": date.today().isoformat()}}
    write_doc(sync.catalog_dir, "missiongrp", doc)
    return {"kind": "missiongrp", "guid": guid, "name": name}


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


def capturable_maps(sync) -> list[dict]:
    """Mapas del robot de `sync` sin GUID de la flota, con su site y cuántas
    positions y zonas tienen, y `captured_as` si ya se capturaron. Se
    recorren los sites (`sessions/<guid>/maps`): el listado de mapas no dice
    su site y pedir cada mapa traería su imagen. Los de fábrica, no."""
    cat = sync.catalog()
    # Lo capturado antes de este robot. Los del principio (fase 0) no apuntan
    # el GUID que tenían en el robot: esos no cuentan.
    captured = {d["_origen"]["guid"]: g for g, d in cat.items.items()
                if cat.kind(g) == "map" and d.get("_origen", {}).get("robot") == sync.serial
                and d["_origen"].get("guid")}
    out = []
    with sync.lock:
        c = sync.client
        for s in c._req("GET", "sessions"):
            if s["guid"].startswith(FACTORY):
                continue
            for m in c._req("GET", f"sessions/{s['guid']}/maps"):
                if is_std(m["guid"]) or m["guid"].startswith(FACTORY):
                    continue
                out.append({"guid": m["guid"], "name": m["name"], "site": s["name"], "site_guid": s["guid"],
                            "positions": len(c._req("GET", f"maps/{m['guid']}/positions")),
                            "zones": len(c._req("GET", f"maps/{m['guid']}/zones")),
                            "captured_as": captured.get(m["guid"]),
                            "url": f"http://{c.host}/maps/{m['guid']}"})
    return out


def capture_map(sync, source: str, name: str = "", session_guid: str | None = None, new_session: str = "",
                others: Iterable[str] = ()) -> dict:
    """Lleva al catálogo el mapa `source` del robot (sin GUID de la flota) con
    sus positions y sus zonas. Va al site `session_guid` del catálogo o a uno
    nuevo `new_session`; si ese mapa ya se capturó de este robot, se actualiza
    su copia (mismo site, mismos GUIDs). Solo escribe en el catálogo."""
    if is_std(source):
        raise CreateError("ese mapa ya tiene GUID de la flota: está en el catálogo")
    cat = sync.catalog()
    mine = lambda g: (cat.items[g].get("_origen", {}).get("robot") == sync.serial  # noqa: E731
                      and cat.items[g]["_origen"].get("guid"))
    prev = next((g for g in cat.items if cat.kind(g) == "map" and mine(g) == source), None)
    with sync.lock:
        c = sync.client
        m = _get_or(c, f"maps/{source}", f"el mapa {source} no está en {sync.serial}")
        png = base64.b64decode(m["base_map"])
        width, height = png_size(png)
        rows = [c._req("GET", f"positions/{p['guid']}") for p in c._req("GET", f"maps/{source}/positions")]
        offsets = {p["guid"]: [c._req("GET", f"docking_offsets/{o['guid']}")
                               for o in c._req("GET", f"positions/{p['guid']}/docking_offsets")] for p in rows}
        zones = [c._req("GET", f"zones/{z['guid']}") for z in c._req("GET", f"maps/{source}/zones")]
    name = (name or m["name"]).strip()
    every = {**cat.items, **cat.drafts}
    taken = set(every) | _retired(sync) | set(others)
    if prev:                                   # volver a capturar: la misma copia
        guid, session = prev, cat.items[cat.items[prev]["session_id"]]
        new_site = False
    else:
        if any(cat.kind(g) == "map" and d["name"] == name for g, d in every.items()):
            raise CreateError(f"ya hay un mapa {name!r} en el catálogo: ponle otro nombre")
        if session_guid:
            if session_guid not in cat.items or cat.kind(session_guid) != "session":
                raise CreateError(f"el site {session_guid} no está en el catálogo")
            session, new_site = cat.items[session_guid], False
        else:
            created = create_session(sync, new_session, others=others)
            cat = sync.catalog()
            session, new_site = cat.items[created["guid"]], True
            taken.add(created["guid"])
        _, inst, _, session_num = parse_std(session["guid"])
        guid = next_guid("map", inst, session_num, taken, per_parent=False)
    _, inst, _, map_num = parse_std(guid)
    today = date.today().isoformat()
    origen = lambda robot_guid: {"robot": sync.serial, "mapa": m["name"], "guid": robot_guid,  # noqa: E731
                                 "capturado": today}
    docs: list[tuple[str, dict]] = [("map", {
        "guid": guid, "name": name if not prev else cat.items[prev]["name"], "session_id": session["guid"],
        **{k: float(m.get(k) or 0.0) for k in ("resolution", "origin_x", "origin_y", "origin_theta")},
        "base_map": f"{guid}.png", "_origen": origen(source)})]

    # GUID del robot → del catálogo: lo ya capturado conserva el suyo; lo nuevo,
    # el siguiente número del mapa. Primero todas, para traducir parent_id.
    def renumber(kind: str, robot_guids: list[str]) -> dict[str, str]:
        known = {mine(g): g for g in cat.items
                 if cat.kind(g) == kind and cat.items[g].get("map_id") == guid and mine(g)}
        out = {}
        for rg in robot_guids:
            out[rg] = known.get(rg) or next_guid(kind, inst, map_num, taken, per_parent=True)
            taken.add(out[rg])
        return out
    pos_guid = renumber("position", [p["guid"] for p in rows])
    for p in rows:
        docs.append(("position", {
            "guid": pos_guid[p["guid"]], "name": p["name"], "type_id": p["type_id"],
            **{k: p[k] for k in ("pos_x", "pos_y", "orientation")}, "map_id": guid,
            "parent_id": pos_guid.get(p["parent_id"]) if p.get("parent_id") else None,
            "docking_offsets": [{k: o.get(k) for k in OFFSET_KEYS} for o in offsets[p["guid"]][:1]],
            # La entrada de un marcador o cargador la crea el MiR (decisión 77).
            "creada_por_mir": p["type_id"] in AUTO_CHILD_TYPES, "_origen": origen(p["guid"])}))
    # Zonas: una acción solo puede usar recursos de fábrica o del catálogo (un
    # sonido subido solo a ese robot no existiría en los demás). La que no
    # cumple se salta y se dice, sin perder el resto del mapa.
    known_sounds = {mine(g): g for g in cat.items if cat.kind(g) == "sound" and mine(g)}
    skipped, ok_zones = [], []
    for z in zones:
        doc = normalized_zone(z)
        bad = []
        for a in doc["actions"]:
            for p in a["parameters"]:
                v = p["value"]
                if isinstance(v, str) and GUIDISH.match(v) and not v.startswith(FACTORY) and not is_std(v):
                    if v in known_sounds:
                        p["value"] = known_sounds[v]
                    else:
                        bad.append(v)
        if bad:
            skipped.append(f"zona {z['name']!r}: usa {', '.join(bad)}, que no está en el catálogo")
        else:
            ok_zones.append((z["guid"], doc))
    zone_guid = renumber("zone", [rg for rg, _ in ok_zones])
    for rg, doc in ok_zones:
        docs.append(("zone", {"guid": zone_guid[rg], **doc, "map_id": guid, "_origen": origen(rg)}))
    gone = sorted(d["name"] for g, d in cat.items.items()
                  if cat.kind(g) in ("position", "zone") and d.get("map_id") == guid
                  and mine(g) not in {*pos_guid, *zone_guid})

    folder = Path(sync.catalog_dir) / FOLDERS["map"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{guid}.png").write_bytes(png)
    for k, d in docs:
        write_doc(sync.catalog_dir, k, d)
    return {"kind": "map", "guid": guid, "name": docs[0][1]["name"], "site": session["name"],
            "new_site": new_site, "updated": bool(prev), "size": [width, height],
            "meters": [round(width * docs[0][1]["resolution"], 2), round(height * docs[0][1]["resolution"], 2)],
            "positions": len(rows), "zones": len(ok_zones), "skipped": skipped, "gone": gone,
            "source": m["name"]}


AFFIX_AT = ("prefix", "suffix")


def affixed(name: str, affix: str, at: str) -> str:
    """El nombre de una position copiada: `affix` delante o detrás, tal cual
    (el separador, si se quiere, va en el propio texto: «pruebas-», «_2»)."""
    return (affix + name if at == "prefix" else name + affix).strip()


def copy_map(sync, source: str, name: str, affix: str, at: str = "suffix", session_guid: str | None = None,
             others: Iterable[str] = (), other_names: Mapping[str, tuple[str, str]] | None = None) -> dict:
    """Copia en el catálogo el mapa `source` del catálogo con su imagen, su
    origen, sus positions y sus zonas, todo con GUIDs nuevos (#33). Las
    missions no se copian: las que llevan positions fijas siguen siendo del
    mapa original (decisión 131).

    Las positions copiadas llevan `affix` como prefijo o sufijo (`at`): su
    nombre no puede repetir el de ninguna otra, de ningún mapa ni robot
    (decisión 174; `other_names` lo junta el servidor de todos los robots
    que respondan). Las zonas conservan el nombre: la web del MiR las lista
    por mapa, y la vuelta a la espera busca su zona por nombre en el mapa
    activo. Va al site `session_guid` (por defecto, el del original). Solo
    escribe en el catálogo; llega a los robots por Sincronización.
    `others`: GUIDs estándar de mapas de los robots."""
    name = (name or "").strip()
    if not name:
        raise CreateError("falta el nombre del mapa nuevo")
    if at not in AFFIX_AT:
        raise CreateError(f"¿prefijo o sufijo? ({at!r} no es ninguno de los dos)")
    if not (affix or "").strip():
        raise CreateError("falta el texto que se añade al nombre de las positions copiadas: sin él, se "
                          "repetirían los nombres del original (decisión 174)")
    cat = sync.catalog()
    src = cat.items.get(source)
    if src is None or cat.kind(source) != "map":
        raise CreateError(f"el mapa {source} no está en el catálogo")
    try:
        png = cat.map_png(source)
    except (KeyError, OSError):
        raise CreateError(f"el mapa {src['name']!r} no tiene su imagen en el catálogo ({src.get('base_map')})") from None
    try:                             # solo para decir el tamaño: la imagen ya se comprobó al entrar
        size = list(png_size(png))
    except CreateError:
        size = None
    every = {**cat.items, **cat.drafts}
    if any(cat.kind(g) == "map" and fold_name(d["name"]) == fold_name(name) for g, d in every.items()):
        raise CreateError(f"ya hay un mapa {name!r} en el catálogo")
    session_guid = session_guid or src["session_id"]
    if session_guid not in cat.items or cat.kind(session_guid) != "session":
        raise CreateError(f"el site {session_guid} no está en el catálogo")
    session = cat.items[session_guid]

    # Lo del mapa original que está en el catálogo (los borradores, no: aún no
    # están terminados). Las entradas que crea el MiR (decisión 77) van con su
    # madre: se copian, y llevan su mismo nombre.
    on_map = lambda kind: sorted((d for g, d in cat.items.items()  # noqa: E731
                                  if cat.kind(g) == kind and d.get("map_id") == source), key=lambda d: d["guid"])
    positions, zones = on_map("position"), on_map("zone")
    new_names = {p["guid"]: affixed(p["name"], affix, at) for p in positions}
    clashes, seen = [], {}
    for p in positions:
        if p.get("creada_por_mir"):
            continue
        new = new_names[p["guid"]]
        where = _name_in_use(new, every, cat, {}, other_names or {})
        if where:
            clashes.append(f"«{new}» (ya está en {where[1]})")
        elif fold_name(new) in seen:
            clashes.append(f"«{new}» (sale dos veces: {seen[fold_name(new)]!r} y {p['name']!r} en el original)")
        seen.setdefault(fold_name(new), p["name"])
    if clashes:
        shown = "; ".join(clashes[:5]) + (f"; y {len(clashes) - 5} más" if len(clashes) > 5 else "")
        raise CreateError(f"con ese {'prefijo' if at == 'prefix' else 'sufijo'} se repetirían nombres de position: "
                          f"{shown}. Prueba con otro texto")

    taken = set(every) | _retired(sync) | set(others)
    _, inst, _, session_num = parse_std(session_guid)
    guid = next_guid("map", inst, session_num, taken, per_parent=False)
    taken.add(guid)
    _, _, _, map_num = parse_std(guid)
    today = date.today().isoformat()
    # Como un mapa subido, el nuevo es «creado desde aquí» (`_origen.guid` = el
    # suyo): sale en Crear y se puede borrar. Sus positions y zonas no: van y
    # vienen con el mapa (decisión 133).
    docs: list[tuple[str, dict]] = [("map", {
        "guid": guid, "name": name, "session_id": session_guid,
        **{k: float(src.get(k) or 0.0) for k in ("resolution", "origin_x", "origin_y", "origin_theta")},
        "base_map": f"{guid}.png", "_origen": {"copia_de": source, "guid": guid, "capturado": today}})]
    copied = lambda old: {"copia_de": old, "mapa": src["name"], "capturado": today}  # noqa: E731

    # Primero todos los GUIDs nuevos, para traducir el parent_id de las entradas.
    new_guid = {}
    for p in positions:
        new_guid[p["guid"]] = next_guid("position", inst, map_num, taken, per_parent=True)
        taken.add(new_guid[p["guid"]])
    for p in positions:
        doc = {k: v for k, v in p.items() if k not in ("_origen", "borrador")}
        doc.update(guid=new_guid[p["guid"]], name=new_names[p["guid"]], map_id=guid,
                   parent_id=new_guid.get(p["parent_id"]) if p.get("parent_id") else None,
                   _origen=copied(p["guid"]))
        docs.append(("position", doc))
    for z in zones:
        zg = next_guid("zone", inst, map_num, taken, per_parent=True)
        taken.add(zg)
        doc = {k: v for k, v in z.items() if k not in ("_origen", "borrador")}
        doc.update(guid=zg, map_id=guid, _origen=copied(z["guid"]))
        docs.append(("zone", doc))

    folder = Path(sync.catalog_dir) / FOLDERS["map"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{guid}.png").write_bytes(png)
    for k, d in docs:
        write_doc(sync.catalog_dir, k, d)
    example = next((p for p in positions if not p.get("creada_por_mir")), None)
    return {"kind": "map", "guid": guid, "name": name, "site": session["name"], "copied_from": src["name"],
            "positions": sum(1 for p in positions if not p.get("creada_por_mir")), "zones": len(zones),
            "example": [example["name"], new_names[example["guid"]]] if example else None,
            "size": size, "meters": [round(n * docs[0][1]["resolution"], 2) for n in size] if size else None}


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


def _capture_mission(sync, cat: Catalog, old: dict, source: str | None = None,
                     translate_known: bool = True) -> tuple[str, dict]:
    """Documento del catálogo para `old` con lo que hay en el robot. Se lee
    la mission `source` (adoptar) o, por defecto, la del mismo GUID.
    `translate_known`: un GUID propio del robot que ya se capturó de él
    (p. ej. una position del mapa antiguo) se cambia por el del catálogo.
    Al recapturar no: una mission de la flota que apunta al original de una
    position es justo el error de la decisión 174, y se dice."""
    c, guid = sync.client, source or old["guid"]
    m = _get_or(c, f"missions/{guid}", f"{old['name'] if not source else guid!r} ya no está en {sync.serial}")
    # GUID del robot → del catálogo, para lo capturado antes de este mismo robot
    # (positions del mapa antiguo, sub-missions...).
    known = {d["_origen"]["guid"]: g for g, d in cat.items.items()
             if translate_known and d.get("_origen", {}).get("robot") == sync.serial and d["_origen"].get("guid")}
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


# --------------------------------------------------------------- recapturar
RECAPTURABLE = ("mission", "position", "zone", "map")


def recapture(sync, guid: str) -> dict:
    """Trae al catálogo la versión que tiene el robot de `sync` de un recurso
    que ya está en el catálogo (#19): una mission, position, zona o mapa
    editado en la web de ese robot (un marcador recalibrado, una mission
    cambiada en el Blockly, una pared nueva en el mapa). Valga o no lo
    creado desde el FM: se lee el mismo GUID de la flota en ese robot.

    Solo lee el robot. Después, los demás robots tienen la versión antigua
    y en Sincronización la ven distinta. El `_origen` no se toca (dice cómo
    entró en el catálogo y decide qué sale en «Creado desde aquí»): se
    apunta `_recapturado` con el robot y la fecha. De un mapa se traen la
    imagen, la resolución, el origen y el nombre; sus positions y zonas se
    traen una a una."""
    cat = sync.catalog()
    old = cat.items.get(guid)
    if old is None:
        raise CreateError(f"{guid} no está en el catálogo" + (" (es un borrador: captúralo en «Crear o importar "
                                                             "recurso»)" if guid in cat.drafts else ""))
    kind = cat.kind(guid)
    if kind not in RECAPTURABLE:
        raise CreateError(f"un recurso de tipo {kind} no se trae del robot: solo missions, positions, zonas y mapas")
    if old.get("creada_por_mir"):
        # La entrada de un marcador o cargador va con su madre: se trae la
        # madre, que se lleva también la entrada (en la vista previa, la
        # entrada movida en el robot sale «corregir», no «distinta»).
        mother = cat.items.get(old.get("parent_id") or "")
        if mother is None:
            raise CreateError(f"la entrada de {old['name']!r} no tiene su marcador en el catálogo")
        guid, old = mother["guid"], mother
    png = None
    with sync.lock:
        if kind == "mission":
            docs = [_capture_mission(sync, cat, old, translate_known=False)]
        elif kind == "position":
            docs = _capture_position(sync, cat, old)
        elif kind == "zone":
            docs = [_capture_zone(sync, cat, old)]
        else:
            doc, png = _recapture_map(sync, cat, old)
            docs = [("map", doc)]
    main = docs[0][1]
    if kind == "position" and fold_name(main["name"]) != fold_name(old["name"]):
        # Renombrada en la web del MiR: el nombre nuevo tampoco puede repetirse
        # en el catálogo (decisión 174). La propia y su entrada no cuentan.
        mine = {guid} | {g for g, d in cat.items.items() if d.get("parent_id") == guid}
        every = {g: d for g, d in {**cat.items, **cat.drafts}.items() if g not in mine}
        where = _name_in_use(main["name"], every, cat, {}, {})
        if where:
            raise CreateError(f"en {sync.serial} se llama {main['name']!r}, y ya hay una position con ese nombre en "
                              f"{where[1]}: cámbiaselo en su web y vuelve a traerla")
    stamp = {"robot": sync.serial, "fecha": date.today().isoformat()}
    for _, d in docs:
        before = cat.items.get(d["guid"])
        if before is not None and "_origen" in before:
            d["_origen"] = before["_origen"]
        d["_recapturado"] = stamp
    skip = {"_origen", "_recapturado", "borrador"}
    changed = sorted(k for k in {*main, *old} - skip if main.get(k) != old.get(k))
    if png is not None:
        if png != cat.map_png(guid):
            changed.append("imagen")
        (Path(sync.catalog_dir) / FOLDERS["map"] / old["base_map"]).write_bytes(png)
    strip = lambda d: {k: v for k, v in (d or {}).items() if k not in skip}  # noqa: E731
    if kind == "position" and any(strip(cat.items.get(d["guid"])) != strip(d) for _, d in docs[1:]):
        changed.append("entrada")
    for k, d in docs:
        write_doc(sync.catalog_dir, k, d)
    return {"kind": kind, "guid": guid, "name": main["name"], "robot": sync.serial, "changed": changed}


def _recapture_map(sync, cat: Catalog, old: dict) -> tuple[dict, bytes]:
    c, guid = sync.client, old["guid"]
    m = _get_or(c, f"maps/{guid}", f"{old['name']!r} no está en {sync.serial}")
    if not m.get("base_map"):
        raise CreateError(f"{sync.serial} no ha dado la imagen del mapa {old['name']!r}")
    png = base64.b64decode(m["base_map"])
    png_size(png)
    name = (m.get("name") or old["name"]).strip()
    if fold_name(name) != fold_name(old["name"]) and any(
            cat.kind(g) == "map" and g != guid and fold_name(d["name"]) == fold_name(name)
            for g, d in {**cat.items, **cat.drafts}.items()):
        raise CreateError(f"en {sync.serial} se llama {name!r}, y ya hay otro mapa con ese nombre en el catálogo")
    doc = {k: v for k, v in old.items() if k != "borrador"}
    doc.update(name=name, **{k: float(m.get(k) or 0.0) for k in ("resolution", "origin_x", "origin_y", "origin_theta")})
    return doc, png


# ----------------------------------------- traer lo hecho en la web del robot
FOREIGN_KINDS = ("position", "zone", "mission")


def foreign(sync) -> list[dict]:
    """Lo que el robot de `sync` tiene **con su GUID propio** dentro de lo de
    la flota (#19): positions y zonas hechas en su web sobre un mapa del
    catálogo y missions en un grupo del catálogo. Las entradas de marcadores
    y cargadores no: las crea el MiR y van con su madre. Cada una con
    `captured_as` si ya se trajo antes (el original sigue en el robot).
    Los mapas hechos en su web van aparte (`capturable_maps`)."""
    cat = sync.catalog()
    maps = {g: d for g, d in cat.items.items() if cat.kind(g) == "map"}
    groups = {g: d for g, d in cat.items.items() if cat.kind(g) == "missiongrp"}
    brought = {d["_origen"]["guid"]: g for g, d in cat.items.items()
               if d.get("_origen", {}).get("robot") == sync.serial and d["_origen"].get("guid")
               and not is_std(d["_origen"]["guid"])}
    out = []
    with sync.lock:
        c = sync.client
        robot_maps = {m["guid"] for m in c._req("GET", "maps")}
        for mg in sorted(set(maps) & robot_maps):
            for p in c._req("GET", f"maps/{mg}/positions"):
                if is_std(p["guid"]) or p["guid"].startswith(FACTORY) or p.get("type_id") in AUTO_CHILD_TYPES:
                    continue
                out.append({"kind": "position", "guid": p["guid"], "name": p["name"], "where": maps[mg]["name"],
                            "parent": mg, "type_id": p.get("type_id"), "captured_as": brought.get(p["guid"]),
                            "url": f"http://{c.host}/maps/{mg}"})
            for z in c._req("GET", f"maps/{mg}/zones"):
                if is_std(z["guid"]) or z["guid"].startswith(FACTORY):
                    continue
                out.append({"kind": "zone", "guid": z["guid"], "name": z["name"], "where": maps[mg]["name"],
                            "parent": mg, "type_id": z.get("type_id"), "captured_as": brought.get(z["guid"]),
                            "url": f"http://{c.host}/maps/{mg}"})
        for gg in sorted(groups):
            try:
                rows = c._req("GET", f"mission_groups/{gg}/missions")
            except MirApiError as e:
                if e.status == 404:          # el robot no tiene ese grupo
                    continue
                raise
            for m in rows:
                if is_std(m["guid"]) or m["guid"].startswith(FACTORY):
                    continue
                out.append({"kind": "mission", "guid": m["guid"], "name": m["name"], "where": groups[gg]["name"],
                            "parent": gg, "captured_as": brought.get(m["guid"]), "url": sync.web_url(m["guid"])})
    return out


def robot_users(client, guids: set[str]) -> list[str]:
    """Missions del robot que usan alguno de `guids` en sus actions (una
    position como destino, una sub-mission con `load_mission`...). Lee las
    actions de todas: ~1,5 s con 70 missions."""
    users = []
    for m in client._req("GET", "missions"):
        if m["guid"] in guids:
            continue
        for a in client._req("GET", f"missions/{m['guid']}/actions"):
            if any(p.get("value") in guids for p in a.get("parameters", [])):
                users.append(m["name"])
                break
    return sorted(users)


def bring(sync, kind: str, source: str, others=lambda kind, parent: set(),
          other_names: Mapping[str, tuple[str, str]] | None = None, config_refs: Iterable[str] = ()) -> dict:
    """Trae al catálogo, **con GUID del proyecto**, la position, zona o
    mission `source` hecha en la web del robot de `sync` (#19), y la
    sustituye en ese robot: crea la copia con el GUID nuevo y borra el
    original (el MiR no deja cambiar el GUID). Decisión del usuario: lo hace
    el FM, no se deja para la web del MiR.

    No toca nada si algo del robot usa el original (una mission de su web,
    `robot_users`) o si es la mission de una action de `fleet.yaml`
    (`config_refs`): habría que cambiarlo a mano primero. Si ya se trajo
    antes (`_origen`), conserva su GUID. Una position no puede repetir el
    nombre de otra del catálogo ni de otro robot (decisión 174); en este,
    el original y su entrada no cuentan. `others(kind, parent)`: GUIDs
    estándar de los demás robots, para no repetir número.

    El robot tiene que estar libre: lo comprueba el servidor."""
    if kind not in FOREIGN_KINDS:
        raise CreateError(f"no se trae un recurso de tipo {kind!r} así (los mapas: «Capturar de un robot»)")
    if is_std(source):
        raise CreateError("ya tiene GUID de la flota: si en el robot es distinto, se trae desde la vista previa")
    cat = sync.catalog()
    every = {**cat.items, **cat.drafts}
    prev = next((g for g, d in cat.items.items() if d.get("_origen", {}).get("robot") == sync.serial
                 and d["_origen"].get("guid") == source), None)
    with sync.lock:
        c = sync.client
        if kind == "position":
            docs, name = _bring_position(sync, cat, every, source, prev, others, other_names or {})
        elif kind == "zone":
            docs, name = _bring_zone(sync, cat, every, source, prev, others)
        else:
            docs, name = _bring_mission(sync, cat, every, source, prev, others)
        if source in set(config_refs):
            raise CreateError(f"{name!r} es la mission de una action de fleet.yaml por su GUID propio: cámbiala en "
                              "Configuración a la del catálogo cuando esté, y vuelve a traerla")
        olds = {source} | ({h["guid"] for h in c._req("GET", f"positions/{source}/helper_positions")}
                           if kind == "position" and docs[1:] else set())
        users = robot_users(c, olds)
        if users:
            raise CreateError(f"en {sync.serial}, {'la usa' if len(users) == 1 else 'la usan'} "
                              f"{', '.join(repr(u) for u in users)}: cámbialas en su web para que usen otra cosa "
                              "(o tráelas también) y vuelve a intentarlo. No se ha tocado nada")
    for k, d in docs:
        write_doc(sync.catalog_dir, k, d)
    guid = docs[0][1]["guid"]
    out = {"kind": kind, "guid": guid, "name": name, "source": source, "robot": sync.serial, "again": bool(prev),
           "replaced": False}
    # En el robot: primero la copia, luego se borra el original. Si la copia
    # falla, el original se queda (el catálogo ya la tiene: se puede reintentar).
    ops = sync.deploy([guid])
    failed = [f"{o.name}: {o.detail}" for o in ops if o.status == "error"]
    if failed:
        out["error"] = "no se ha podido crear la copia en el robot (" + "; ".join(failed) + "): el original se queda"
        return out
    with sync.lock:                    # al borrar un marcador, el MiR borra su entrada
        sync.client._req("DELETE", f"{FOLDERS[kind]}/{source}")
    out["replaced"] = True
    return out


def _bring_position(sync, cat, every, source, prev, others, other_names):
    c = sync.client
    p = _get_or(c, f"positions/{source}", f"la position {source} ya no está en {sync.serial}")
    map_guid = p.get("map_id") or str(p.get("map") or "").rstrip("/").split("/")[-1]
    if map_guid not in cat.items or cat.kind(map_guid) != "map":
        raise CreateError(f"{p['name']!r} está en un mapa que no es del catálogo: captura el mapa entero")
    if p["type_id"] in AUTO_CHILD_TYPES:
        raise CreateError(f"la entrada de {p['name']!r} la crea el MiR: se trae con su marcador o cargador")
    offsets = [c._req("GET", f"docking_offsets/{o['guid']}")
               for o in c._req("GET", f"positions/{source}/docking_offsets")]
    helpers = [c._req("GET", f"positions/{h['guid']}") for h in c._req("GET", f"positions/{source}/helper_positions")]
    # Decisión 174, sin contarse a sí misma: ni el original y su entrada en
    # este robot, ni lo que ya se trajo (y su entrada) en el catálogo.
    mine_cat = {prev} | {g for g, d in cat.items.items() if prev and d.get("parent_id") == prev}
    skip = {source} | {h["guid"] for h in helpers} | mine_cat
    maps = {m["guid"]: m["name"] for m in c._req("GET", "maps")}
    robot = {}
    for q in c._req("GET", "positions"):
        if q["guid"] in skip:
            continue
        mg = q.get("map_id") or str(q.get("map") or "").rstrip("/").split("/")[-1]
        robot.setdefault(fold_name(q["name"]), (q["name"], f"{sync.serial} (mapa {maps.get(mg, mg)!r})"))
    where = _name_in_use(p["name"], {g: d for g, d in every.items() if g not in mine_cat}, cat, robot, other_names)
    if where:
        raise CreateError(f"ya hay una position {where[0]!r} en {where[1]}: cámbiale el nombre a {p['name']!r} en la "
                          "web del MiR y vuelve a traerla (la web lista las de todos los mapas juntas)")
    _, inst, _, map_num = parse_std(map_guid)
    taken = set(every) | _retired(sync) | robot_std_guids(c, "position", map_guid) | set(others("position", map_guid))
    guid = prev or next_guid("position", inst, map_num, taken, per_parent=True)
    taken.add(guid)
    mapa = cat.items[map_guid]["name"]
    docs = [("position", {
        "guid": guid, "name": p["name"], "type_id": p["type_id"],
        **{k: p[k] for k in ("pos_x", "pos_y", "orientation")}, "map_id": map_guid, "parent_id": None,
        "docking_offsets": [{k: o.get(k) for k in OFFSET_KEYS} for o in offsets[:1]],
        "creada_por_mir": False, "_origen": _origen(sync.serial, source, mapa=mapa)})]
    if p["type_id"] in MARKER_TYPES and helpers:
        e = helpers[0]
        eg = next((g for g in mine_cat if g and g != prev), None) or next_guid("position", inst, map_num, taken,
                                                                             per_parent=True)
        docs.append(("position", {
            "guid": eg, "name": e["name"], "type_id": e["type_id"],
            **{k: e[k] for k in ("pos_x", "pos_y", "orientation")}, "map_id": map_guid, "parent_id": guid,
            "docking_offsets": [], "creada_por_mir": True, "_origen": _origen(sync.serial, e["guid"], mapa=mapa)}))
    return docs, p["name"]


def _bring_zone(sync, cat, every, source, prev, others):
    c = sync.client
    z = _get_or(c, f"zones/{source}", f"la zona {source} ya no está en {sync.serial}")
    map_guid = z["map_id"]
    if map_guid not in cat.items or cat.kind(map_guid) != "map":
        raise CreateError(f"{z['name']!r} está en un mapa que no es del catálogo: captura el mapa entero")
    doc = normalized_zone(z)
    for a in doc["actions"]:
        for q in a["parameters"]:
            v = q["value"]
            if isinstance(v, str) and GUIDISH.match(v) and not v.startswith(FACTORY) and v not in cat.items:
                raise CreateError(f"la zona {z['name']!r} usa {v}, que no está en el catálogo (¿un sonido subido "
                                  "solo a este robot?): súbelo al catálogo o cambia la acción en su web")
    _, inst, _, map_num = parse_std(map_guid)
    taken = set(every) | _retired(sync) | robot_std_guids(c, "zone", map_guid) | set(others("zone", map_guid))
    guid = prev or next_guid("zone", inst, map_num, taken, per_parent=True)
    return [("zone", {"guid": guid, **doc,
                      "_origen": _origen(sync.serial, source, mapa=cat.items[map_guid]["name"])})], z["name"]


def _bring_mission(sync, cat, every, source, prev, others):
    c = sync.client
    m = _get_or(c, f"missions/{source}", f"la mission {source} ya no está en {sync.serial}")
    group = cat.items.get(m["group_id"])
    if group is None or cat.kind(m["group_id"]) != "missiongrp":
        raise CreateError(f"{m['name']!r} está en un grupo que no es del catálogo: muévela en su web a uno que sí")
    same = [d["name"] for g, d in every.items() if cat.kind(g) == "mission" and g != prev
            and d.get("group_id") == m["group_id"] and fold_name(d["name"]) == fold_name(m["name"])]
    if same:
        raise CreateError(f"ya hay una mission {same[0]!r} en el grupo {group['name']!r} del catálogo. Si esta es "
                          "una versión nueva de aquella, usa «Adoptar» en «Crear o importar recurso»; si no, "
                          "cámbiale el nombre en su web")
    if prev:
        old = cat.items[prev]
    else:
        _, inst, _, group_num = parse_std(m["group_id"])
        taken = set(every) | _retired(sync) | robot_std_guids(c, "mission") | set(others("mission", ""))
        guid = next_guid("mission", inst, group_num, taken, per_parent=False)
        # La session: la que tiene en el robot si es del catálogo; si no, la de su grupo.
        sid = m.get("session_id")
        session = sid if sid in cat.items and cat.kind(sid) == "session" else group_session(cat, m["group_id"], None)
        old = {"guid": guid, "name": m["name"], "description": "", "group_id": m["group_id"],
               "session_id": session, "hidden": False}
    return [_capture_mission(sync, cat, old, source=source)], m["name"]


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


def adopt(sync, source: str, target: str | None = None, others: Iterable[str] = (),
          site: str | None = None) -> dict:
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
            session = group_session(cat, m["group_id"], site)
            _, inst, _, group_num = parse_std(m["group_id"])
            taken = set(every) | _retired(sync) | robot_std_guids(c, "mission") | set(others)
            guid = next_guid("mission", inst, group_num, taken, per_parent=False)
            old = {"guid": guid, "name": m["name"], "description": "", "group_id": m["group_id"],
                   "session_id": session, "hidden": False}
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
    if d is None or not is_std(guid) or cat.kind(guid) not in ("mission", "position", "zone", "map", "session",
                                                               "missiongrp"):
        return None
    return d if d.get("_origen", {}).get("guid") == guid else None


def delete_blockers(sync, guid: str, definitions: dict, wanted: Iterable[str]) -> list[str]:
    """Por qué no se puede borrar `guid` (vacío = se puede): solo lo creado
    aquí y que nadie use (`remove.usage_blockers`)."""
    cat = sync.catalog()
    if created_here(cat, guid) is None:
        return ["solo se borra desde aquí lo creado en «Crear o importar recurso» (en Catálogo)"]
    return usage_blockers(sync, cat, guid, definitions, wanted)
