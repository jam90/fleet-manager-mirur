"""Sincronización del catálogo (`catalog/`) con un MiR (docs/plan-admin.md §3).

El catálogo guarda sessions, mapas, positions, sonidos, grupos y missions
con **GUIDs comunes en toda la flota** (§4). Este módulo lleva a un robot
una **selección** de ese catálogo:

1. `Catalog.closure(seleccion)` añade lo que la selección necesita (una
   mission arrastra su grupo, su session, sus positions, sus sub-missions y
   sus sonidos; una position, su mapa) y lo ordena por dependencias.
2. `fetch_view(client, ...)` lee del robot solo lo que hace falta comparar.
3. `plan(...)` — **puro, sin red** — compara y decide una operación por
   recurso: crear, corregir, igual (no se toca) o distinta (no se toca).
4. `apply(...)` ejecuta las operaciones por REST e informa de cada una.

Así sincronizar una mission suelta es barato: sus dependencias que el robot
ya tiene salen "igual" y no generan escrituras.

Qué lleva GUID nuestro y qué no (decisiones 77 y 78 de docs/avance.md): la
entrada de un marcador o cargador, el docking offset y las actions de una
mission los crea el MiR con sus propios GUIDs; se localizan por su padre.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from fm.adapters.mir.client import MirApiError, MirClient

PREFIX = "imperialfleet"
# Tipos de position que el MiR crea solo al crear su madre (entrada de
# marcador VL = 12, de cargador = 21).
AUTO_CHILD_TYPES = (12, 21)
# Recursos de fábrica: mismo GUID en todos los MiR, se referencian tal cual.
FACTORY = "mirconst-"
GUIDISH = re.compile(r"^[0-9a-z]{8}-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{12}$")
POSE = ("pos_x", "pos_y", "orientation")
POSE_TOL = 1e-3
MAP_FIELDS = ("name", "session_id", "resolution", "origin_x", "origin_y", "origin_theta")

# tipo del GUID → carpeta del catálogo. El orden es el de despliegue.
FOLDERS = {
    "session": "sessions",
    "map": "maps",
    "position": "positions",
    "sound": "sounds",
    "missiongrp": "mission_groups",
    "mission": "missions",
}
KIND_ORDER = {k: i for i, k in enumerate(FOLDERS)}
KIND_LABEL = {"session": "session", "map": "mapa", "position": "position",
              "sound": "sonido", "missiongrp": "grupo", "mission": "mission"}


# --------------------------------------------------------------------- GUIDs
def std_guid(tipo: str, inst: int, padre: int, numero: int) -> str:
    """GUID estándar: `imperialfleet-<tipo relleno a 10>-II-PPPP-NNN` (36 caracteres)."""
    g = f"{PREFIX}-{tipo.ljust(10, '0')}-{inst:02d}-{padre:04d}-{numero:03d}"
    assert len(g) == 36, g
    return g


def parse_std(guid: str) -> tuple[str, int, int, int]:
    """Inversa de std_guid: (tipo, inst, padre, número)."""
    _, tipo, inst, padre, numero = guid.split("-")
    return tipo.rstrip("0"), int(inst), int(padre), int(numero)


def is_std(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX + "-") and len(value) == 36


# ------------------------------------------------------------------ catálogo
@dataclass
class Catalog:
    root: Path
    items: dict[str, dict] = field(default_factory=dict)   # GUID → documento

    @classmethod
    def load(cls, root: Path) -> "Catalog":
        cat = cls(Path(root))
        for folder in FOLDERS.values():
            for f in sorted((cat.root / folder).glob("*.json")):
                d = json.loads(f.read_text(encoding="utf-8"))
                cat.items[d["guid"]] = d
        return cat

    @staticmethod
    def kind(guid: str) -> str:
        return parse_std(guid)[0]

    def entry_of(self, parent: str) -> str | None:
        """Entrada (creada por el MiR) de un marcador o cargador del catálogo."""
        return next((g for g, d in self.items.items()
                     if d.get("creada_por_mir") and d.get("parent_id") == parent), None)

    def deps(self, guid: str) -> set[str]:
        """Recursos del catálogo que tienen que existir antes que `guid`."""
        d, k = self.items[guid], self.kind(guid)
        if k == "map":
            return {d["session_id"]}
        if k == "position":
            return {d["map_id"]} | ({d["parent_id"]} if d.get("parent_id") else set())
        if k == "mission":
            refs = {p["value"] for a in d["actions"] for p in a["parameters"] if is_std(p["value"])}
            return {d["group_id"], d["session_id"]} | refs
        return set()

    def closure(self, selected: Iterable[str]) -> list[str]:
        """Selección + todo lo que necesita, en orden de despliegue."""
        todo, out = list(selected), set()
        while todo:
            g = todo.pop()
            if g in out:
                continue
            if g not in self.items:
                raise ValueError(f"{g} no está en el catálogo")
            out.add(g)
            todo += self.deps(g)
            # Un marcador o cargador arrastra su entrada (hay que dejarla en su sitio).
            child = self.entry_of(g) if self.kind(g) == "position" else None
            if child:
                todo.append(child)
        # Missions: la llamada por load_mission antes que la que llama.
        missions: list[str] = []

        def visit(g: str, path: tuple = ()) -> None:
            if g in missions:
                return
            if g in path:
                raise ValueError("load_mission circular: " + " → ".join(path + (g,)))
            for dep in sorted(x for x in self.deps(g) if self.kind(x) == "mission"):
                visit(dep, path + (g,))
            missions.append(g)
        for g in sorted(x for x in out if self.kind(x) == "mission"):
            visit(g)
        rest = sorted((x for x in out if self.kind(x) != "mission"),
                      key=lambda x: (KIND_ORDER[self.kind(x)], x))
        return rest + missions

    def map_png(self, guid: str) -> bytes:
        return (self.root / "maps" / self.items[guid]["base_map"]).read_bytes()

    def sound_bytes(self, guid: str) -> bytes:
        return (self.root / "sounds" / self.items[guid]["sound"]).read_bytes()

    def summary(self) -> list[dict]:
        """Lista para la UI: un elemento por recurso, en orden de despliegue."""
        out = []
        for g in sorted(self.items, key=lambda x: (KIND_ORDER[self.kind(x)], x)):
            d, k = self.items[g], self.kind(g)
            parent = d.get("group_id") or d.get("map_id") or d.get("session_id")
            extra = ""
            if k == "position":
                extra = f"tipo {d['type_id']}"
            elif k == "mission":
                extra = f"{len(d['actions'])} actions"
            o = d.get("_origen", {})
            out.append({"guid": g, "kind": k, "name": d["name"], "parent": parent, "extra": extra,
                        "auto": bool(d.get("creada_por_mir")), "parent_id": d.get("parent_id"),
                        "origin": o.get("robot", ""), "captured": o.get("capturado", ""),
                        "origin_group": o.get("grupo", "")})
        return out


# ------------------------------------------------------------- vista robot
@dataclass
class RobotView:
    """Lo que hay en el robot de los recursos que interesan (por GUID)."""
    sessions: dict[str, dict] = field(default_factory=dict)
    maps: dict[str, dict] = field(default_factory=dict)
    positions: dict[str, dict] = field(default_factory=dict)   # todas las de los mapas del catálogo
    sounds: dict[str, dict] = field(default_factory=dict)
    groups: dict[str, dict] = field(default_factory=dict)
    missions: dict[str, dict] = field(default_factory=dict)    # {name, group_id, actions normalizadas}

    def entry_of(self, parent: str) -> dict | None:
        return next((p for p in self.positions.values() if p.get("parent_id") == parent), None)


def normalized_actions(client: MirClient, mission_guid: str) -> list[dict]:
    """Actions de una mission del robot con el formato del catálogo: en orden
    de priority, sin GUIDs, y el anidamiento como (priority, parámetro)."""
    acts = sorted((client._req("GET", f"missions/{mission_guid}/actions/{x['guid']}")
                   for x in client._req("GET", f"missions/{mission_guid}/actions")),
                  key=lambda x: x["priority"])
    owner = {p["guid"]: (x["priority"], p["id"]) for x in acts for p in x["parameters"]}
    return [{"priority": x["priority"], "action_type": x["action_type"],
             "scope": ({"action": owner[x["scope_reference"]][0],
                        "parameter": owner[x["scope_reference"]][1]}
                       if x["scope_reference"] else None),
             "parameters": [{"id": p["id"], "value": p["value"], "input_name": p["input_name"]}
                            for p in x["parameters"]]}
            for x in acts]


def fetch_view(client: MirClient, cat: Catalog, guids: list[str]) -> RobotView:
    """Lee del robot lo necesario para planificar `guids` (solo GET)."""
    v = RobotView()
    want = {k: [g for g in guids if cat.kind(g) == k] for k in FOLDERS}
    v.sessions = {s["guid"]: s for s in client._req("GET", "sessions")}
    v.sounds = {s["guid"]: s for s in client._req("GET", "sounds")}
    v.groups = {s["guid"]: s for s in client._req("GET", "mission_groups")}
    robot_maps = {m["guid"] for m in client._req("GET", "maps")}
    maps_needed = {g for g in guids if cat.kind(g) == "map"}
    for g in maps_needed & robot_maps:
        v.maps[g] = client._req("GET", f"maps/{g}")
        for p in client._req("GET", f"maps/{g}/positions"):
            v.positions[p["guid"]] = client._req("GET", f"positions/{p['guid']}")
    robot_missions = {m["guid"] for m in client._req("GET", "missions")}
    for g in set(want["mission"]) & robot_missions:
        m = client._req("GET", f"missions/{g}")
        v.missions[g] = {"name": m["name"], "group_id": m["group_id"],
                         "actions": normalized_actions(client, g)}
    return v


# ----------------------------------------------------------------- plan
@dataclass
class Op:
    guid: str
    kind: str
    name: str
    verb: str                   # crear | corregir | igual | distinta
    detail: str = ""
    warning: str = ""
    status: str = "pendiente"   # pendiente | hecho | error | saltada | sin cambios
    target: str | None = None   # GUID en el robot (entrada de marcador a corregir)

    def to_dict(self) -> dict:
        return asdict(self)


def _pose_differs(a: dict, b: dict) -> bool:
    return any(abs(a[k] - b[k]) > POSE_TOL for k in POSE)


def _fmt_pose(p: dict) -> str:
    return f"({p['pos_x']:.3f}, {p['pos_y']:.3f}, {p['orientation']:.1f}°)"


def plan(cat: Catalog, view: RobotView, guids: list[str], serial: str,
         map_png_b64: Callable[[str], str] | None = None) -> list[Op]:
    """Una operación por recurso de `guids` (ya en orden). Puro: no hay red.

    `map_png_b64(guid)` da la imagen del catálogo en base64 para comparar el
    `base_map`; por defecto la lee del catálogo."""
    png = map_png_b64 or (lambda g: base64.b64encode(cat.map_png(g)).decode())
    ops: list[Op] = []
    for g in guids:
        d, k = cat.items[g], cat.kind(g)
        op = Op(g, k, d["name"], "crear")
        if k in ("session", "missiongrp", "sound"):
            have = {"session": view.sessions, "missiongrp": view.groups, "sound": view.sounds}[k].get(g)
            if have is not None:
                op.verb = "igual" if have.get("name") == d["name"] else "distinta"
                if op.verb == "distinta":
                    op.detail = f"en el robot se llama {have.get('name')!r}"
        elif k == "map":
            have = view.maps.get(g)
            if have is not None:
                diff = [f for f in MAP_FIELDS if have.get(f) != d[f]]
                if have.get("base_map") != png(g):
                    diff.append("imagen")
                op.verb = "distinta" if diff else "igual"
                op.detail = ("difiere: " + ", ".join(diff)) if diff else ""
        elif k == "position" and d.get("creada_por_mir"):
            have = view.entry_of(d["parent_id"])
            if have is None:
                op.detail = "la crea el MiR con su madre; se deja en su sitio"
            else:
                op.target = have["guid"]
                if _pose_differs(have, d):
                    op.verb = "corregir"
                    op.detail = f"mover {_fmt_pose(have)} → {_fmt_pose(d)}"
                else:
                    op.verb = "igual"
        elif k == "position":
            have = view.positions.get(g)
            if have is not None:
                diff = [f for f in ("name", "type_id", "map_id") if have.get(f) != d[f]]
                if _pose_differs(have, d):
                    diff.append("pose")
                op.verb = "distinta" if diff else "igual"
                op.detail = ("difiere: " + ", ".join(diff)) if diff else ""
            if d.get("docking_offsets") and d.get("_origen", {}).get("robot") not in (None, serial):
                op.warning = (f"docking offset calibrado con {d['_origen']['robot']}: "
                              f"comprobar el atraque en {serial}")
        elif k == "mission":
            have = view.missions.get(g)
            if have is None:
                op.detail = f"{len(d['actions'])} actions"
            else:
                diff = [f for f in ("name", "group_id") if have.get(f) != d[f]]
                if have["actions"] != d["actions"]:
                    diff.append("actions")
                op.verb = "distinta" if diff else "igual"
                op.detail = ("difiere: " + ", ".join(diff)) if diff else ""
        if op.verb == "distinta":
            op.detail += " — no se toca (sustituir aún no está implementado)"
        ops.append(op)
    return ops


# ----------------------------------------------------------------- apply
Progress = Callable[[Op], None]


def apply(client: MirClient, cat: Catalog, ops: list[Op], progress: Progress = lambda op: None) -> list[Op]:
    """Ejecuta `ops` en orden. Si una falla, las que dependen de ella se saltan."""
    failed: set[str] = set()
    for op in ops:
        if op.verb in ("igual", "distinta"):
            op.status = "sin cambios"
            progress(op)
            continue
        blocked = cat.deps(op.guid) & failed
        if blocked:
            op.status, op.detail = "saltada", f"falló antes: {', '.join(sorted(blocked))}"
            failed.add(op.guid)
            progress(op)
            continue
        try:
            _apply_one(client, cat, op)
            op.status = "hecho"
        except (MirApiError, OSError, RuntimeError) as e:
            op.status, op.detail = "error", str(e)
            failed.add(op.guid)
        progress(op)
    return ops


def _apply_one(client: MirClient, cat: Catalog, op: Op) -> None:
    d, g = cat.items[op.guid], op.guid
    if op.kind == "session":
        client._req("POST", "sessions", json={"guid": g, "name": d["name"],
                                              "description": d.get("description", "")})
    elif op.kind == "map":
        client._req("POST", "maps", json={
            "guid": g, "session_id": d["session_id"], "name": d["name"],
            "base_map": base64.b64encode(cat.map_png(g)).decode(),
            **{f: d[f] for f in ("resolution", "origin_x", "origin_y", "origin_theta")}})
    elif op.kind == "position" and d.get("creada_por_mir"):
        target = op.target
        if target is None:      # recién creada con su madre: buscarla
            hijas = client._req("GET", f"positions/{d['parent_id']}/helper_positions")
            if not hijas:
                raise RuntimeError("el MiR no ha creado la entrada del marcador")
            target = hijas[0]["guid"]
            op.target = target
        have = client._req("GET", f"positions/{target}")
        if _pose_differs(have, d):
            client._req("PUT", f"positions/{target}", json={k: d[k] for k in POSE})
    elif op.kind == "position":
        body = {k: d[k] for k in ("guid", "name", "type_id", *POSE, "map_id")}
        if d["docking_offsets"]:
            o = d["docking_offsets"][0]       # el MiR exige los offsets al crear un marcador
            body |= {"offset_x": o["x_offset"], "offset_y": o["y_offset"],
                     "offset_orientation": o["orientation_offset"]}
        client._req("POST", "positions", json=body)
    elif op.kind == "sound":
        client._req("POST", "sounds", json={
            "guid": g, "name": d["name"], "note": d.get("note", ""), "volume": d.get("volume", 100),
            "sound": base64.b64encode(cat.sound_bytes(g)).decode()})
    elif op.kind == "missiongrp":
        client._req("POST", "mission_groups", json={"guid": g, "name": d["name"], "priority": 0,
                                                    "feature": "default", "icon": ""})
    elif op.kind == "mission":
        client._req("POST", "missions", json={k: d[k] for k in
                                              ("guid", "name", "description", "group_id", "session_id", "hidden")})
        # El anidamiento apunta al GUID de un parámetro de la action contenedora,
        # que pone el MiR al crearla: se lee y se usa en las de dentro.
        param_guid: dict[tuple[int, str], str] = {}
        for a in d["actions"]:
            body = {"action_type": a["action_type"], "priority": a["priority"],
                    "parameters": a["parameters"]}
            if a["scope"]:
                body["scope_reference"] = param_guid[(a["scope"]["action"], a["scope"]["parameter"])]
            resp = client._req("POST", f"missions/{g}/actions", json=body)
            for p in client._req("GET", f"missions/{g}/actions/{resp['guid']}")["parameters"]:
                param_guid[(a["priority"], p["id"])] = p["guid"]
        if normalized_actions(client, g) != d["actions"]:
            raise RuntimeError("creada, pero al releerla no coincide con el catálogo")


# ------------------------------------------------------ servicio por robot
class MirSync:
    """Lo que ofrece el driver MiR a la web (`driver.make_sync`). Usa su propio
    cliente REST: corre en un hilo aparte del poll de 1 Hz."""

    def __init__(self, serial: str, host: str, auth: str, catalog_dir: Path):
        self.serial = serial
        self.catalog_dir = Path(catalog_dir)
        # Lectura más larga que el poll: un POST /maps sube la imagen entera.
        self.client = MirClient(host, auth, timeout=(2.0, 20.0))

    def catalog(self) -> Catalog:
        # Se relee cada vez: el catálogo cambia al capturar (git, script).
        return Catalog.load(self.catalog_dir)

    def preview(self, selected: Iterable[str]) -> tuple[Catalog, list[Op]]:
        cat = self.catalog()
        guids = cat.closure(selected)
        return cat, plan(cat, fetch_view(self.client, cat, guids), guids, self.serial)

    def deploy(self, selected: Iterable[str], progress: Progress = lambda op: None) -> list[Op]:
        cat, ops = self.preview(selected)   # se replanifica: el robot puede haber cambiado
        return apply(self.client, cat, ops, progress)
