"""Quitar recursos de la flota (decisiones 104–106): lo común a la pestaña
Crear («Borrar» lo creado allí) y a la Sincronización («Quitar de este
robot» y «Retirar de la flota»).

Tres alcances, de menos a más:

- **Quitar de un robot** (solo missions): la borra de ESE robot, que según
  su `fleet.yaml` no la necesita; el catálogo no cambia y se puede volver a
  desplegar. Las positions no: una order puede pedir cualquiera por nombre
  (`target_pos`), así que el FM no sabe si un robot "no la usa".
- **Retirar de la flota** (missions, positions, zonas y mapas del catálogo): la borra de
  todos los robots y del catálogo, y **su número queda reservado** en
  `catalog/retirados.json` (tiene historial en git y puede seguir en
  backups del MiR: reutilizar el GUID para otra cosa confundiría).
- **Borrar lo creado en la pestaña Crear** (`create.py`): igual que
  retirar, pero el número queda libre (son pruebas).

Un mapa se va con sus positions y sus zonas (decisión 133): el MiR las
borra con él (comprobado en mir-1 y mir-2 el 2026-10-29), así que en el
catálogo también.

Lo que no lleva GUID `imperialfleet-…` no es del FM y no se toca nunca.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterable

from fm.adapters.mir.client import MirApiError
from fm.adapters.mir.sync import FOLDERS, Catalog, is_std, presence

RETIRED_FILE = "retirados.json"
REMOVABLE = ("mission", "position", "zone", "map")


def retired(root: Path) -> list[dict]:
    """Registro de lo retirado: [{guid, name, kind, fecha, robots}]."""
    f = Path(root) / RETIRED_FILE
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def entries_of(cat: Catalog, guid: str) -> list[str]:
    """Entradas (creadas por el MiR) de un marcador: se van con él."""
    return [g for g, d in cat.items.items() if d.get("parent_id") == guid and d.get("creada_por_mir")]


def dependents(cat: Catalog, guid: str) -> list[str]:
    """Lo que se va con `guid`: las positions (con sus entradas) y las zonas
    de un mapa, o la entrada de un marcador. Borradores incluidos."""
    if cat.kind(guid) == "map":
        return sorted(g for g, d in {**cat.items, **cat.drafts}.items()
                      if cat.kind(g) in ("position", "zone") and d.get("map_id") == guid)
    return entries_of(cat, guid)


def usage_blockers(sync, cat: Catalog, guid: str, definitions: dict, wanted: Iterable[str]) -> list[str]:
    """Quién usa `guid` en la flota: `fleet.yaml` (catálogo `actions:` o lo
    que piden los robots, p.ej. la mission de carga) y otros recursos del
    catálogo. Vacío = nadie."""
    d = cat.items.get(guid) or cat.drafts.get(guid)
    out = []
    going = {guid, *dependents(cat, guid)}
    wanted = set(wanted)
    refs = wanted | {sync._resolve(cat, w) for w in wanted}       # por GUID o por nombre
    if cat.kind(guid) == "mission":
        names = {n for n, a in (definitions or {}).items() if a and a.get("mission") in (guid, d["name"])}
        if names:
            out.append(f"está en fleet.yaml (actions: {', '.join(sorted(names))}): quítala antes en el Configurador")
        elif guid in refs or d["name"] in wanted:
            out.append("la necesita algún robot según fleet.yaml (¿mission de carga?)")
    elif cat.kind(guid) == "position":
        names = {n for n, a in (definitions or {}).items()
                 if a and d["name"] in (a.get("positions_allowlist") or [])}
        if names:
            out.append(f"está en la allowlist de fleet.yaml (actions: {', '.join(sorted(names))})")
    elif cat.kind(guid) == "map":
        # Una position de la allowlist que solo existe en este mapa se queda sin ella.
        here = {cat.items[g]["name"] for g in going if g in cat.items and cat.kind(g) == "position"}
        elsewhere = {x["name"] for g, x in cat.items.items() if cat.kind(g) == "position" and g not in going}
        names = {n for n, a in (definitions or {}).items()
                 if a and set(a.get("positions_allowlist") or []) & (here - elsewhere)}
        if names:
            out.append(f"tiene positions de la allowlist de fleet.yaml (actions: {', '.join(sorted(names))})")
    users = sorted(cat.items[g]["name"] for g in cat.items if g not in going and going & cat.deps(g))
    if users:
        out.append(f"la usan otros recursos del catálogo: {', '.join(users)}")
    return out


def retire_blockers(sync, guid: str, definitions: dict, wanted: Iterable[str]) -> list[str]:
    cat = sync.catalog()
    if guid not in cat.items or not is_std(guid):
        return [f"{guid} no está en el catálogo"]
    if cat.kind(guid) not in REMOVABLE:
        return ["desde aquí solo se retiran missions, positions, zonas y mapas"]
    if cat.items[guid].get("creada_por_mir"):
        return ["es la entrada de un marcador: se retira con su marcador"]
    return usage_blockers(sync, cat, guid, definitions, wanted)


def remove_blockers(sync, guid: str) -> list[str]:
    """Por qué no se puede quitar `guid` del robot de `sync` (vacío = se puede)."""
    cat = sync.catalog()
    if guid not in cat.items or cat.kind(guid) != "mission":
        return ["de un robot solo se quitan missions del catálogo (una position se retira de la flota)"]
    required, _ = sync.required(cat)
    if guid in required:
        return ["la necesita este robot según su fleet.yaml (una action, la carga o algo que usan)"]
    with sync.lock:
        here = presence(sync.client, cat)
    if not here.get(guid):
        return ["este robot no la tiene"]
    users = sorted(cat.items[g]["name"] for g in cat.items if g != guid and guid in cat.deps(g) and here.get(g))
    if users:
        return [f"la usan en este robot: {', '.join(users)}"]
    return []


def exists_on(client, guid: str) -> bool:
    try:
        client._req("GET", f"{FOLDERS[Catalog.kind(guid)]}/{guid}")
        return True
    except MirApiError as e:
        if e.status == 404:
            return False
        raise


def delete_on(client, guid: str) -> None:
    """Borra el recurso del robot (al borrar un marcador, el MiR borra su entrada)."""
    client._req("DELETE", f"{FOLDERS[Catalog.kind(guid)]}/{guid}")


def forget(sync, guid: str, retire: bool = False, robots: Iterable[str] = ()) -> list[str]:
    """Quita del catálogo el recurso (y la entrada de un marcador). Con
    `retire`, apunta sus GUIDs en `retirados.json` para no reutilizarlos."""
    cat = sync.catalog()
    gone = [guid, *dependents(cat, guid)]
    if retire:
        log = retired(sync.catalog_dir)
        today = date.today().isoformat()
        log += [{"guid": g, "name": (cat.items.get(g) or cat.drafts.get(g) or {}).get("name", ""),
                 "kind": cat.kind(g), "fecha": today, "robots": sorted(robots)} for g in gone]
        (Path(sync.catalog_dir) / RETIRED_FILE).write_text(
            json.dumps(log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for g in gone:
        folder = Path(sync.catalog_dir) / FOLDERS[cat.kind(g)]
        d = cat.items.get(g) or cat.drafts.get(g) or {}
        if d.get("base_map"):                    # un mapa se lleva su imagen
            (folder / d["base_map"]).unlink(missing_ok=True)
        (folder / f"{g}.json").unlink(missing_ok=True)
    return gone
