"""Edición de `fleet.yaml` desde el configurador (docs/plan-admin.md §2.3).

`fleet.yaml` está comentado a propósito (es material didáctico), así que no
se reescribe entero: se sustituye en el texto **solo el valor** de cada clave
editada, usando la posición exacta que da el compositor de PyYAML
(`yaml.compose` → `start_mark`/`end_mark` de cada nodo). Comentarios, orden,
espacios y comillas del resto del fichero quedan intactos, y el diff en git es
justo la línea cambiada (decisión 96: se probó `ruamel.yaml` y normalizaba
`{ a: 1 }` → `{a: 1}` y juntaba listas partidas en varias líneas).

Qué se puede editar (v1, decisión 93): las actions habilitadas y
`battery_min` de cada robot, `auto_charge.*` y `assigner.prefer_not_charging`.
El catálogo de actions y las altas y bajas de robots, más adelante.

Guardar (`save`): editar el texto → validar con el MISMO cargador del
arranque y con el driver de cada robot → copia `.bak` con fecha → escritura
atómica (fichero temporal + `os.replace`). Si algo falla, no se toca el
fichero. Esto corre en el hilo web; aplicar la config nueva es cosa del hilo
principal (`fm/reload.py`).
"""
from __future__ import annotations

import math
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from fm.adapters import make_driver
from fm.config import ConfigError, FleetConfig, load_config

# Claves editables fuera de `robots:`: (bloque, clave) → tipo.
GLOBAL_KEYS: dict[tuple[str, str], type] = {
    ("auto_charge", "battery_floor"): float,
    ("auto_charge", "priority"): int,
    ("auto_charge", "abort_cooldown_s"): float,
    ("assigner", "prefer_not_charging"): bool,
}
ROBOT_KEYS = ("actions", "battery_min")


class EditError(ValueError):
    """Edición no aplicable (clave no editable, valor de tipo incorrecto,
    fichero con una forma que no se sabe editar sin estropearlo)."""


@dataclass
class SaveResult:
    cfg: FleetConfig            # la config nueva, ya validada
    changed: bool               # False = las ediciones no cambiaban nada
    backup: Path | None         # copia del fichero anterior


# --------------------------------------------------------------- ediciones
def normalize(edits: dict) -> list[tuple[tuple[str, ...], object]]:
    """Ediciones de la UI → lista de (ruta en el YAML, valor ya tipado).

    Formato de entrada (todo opcional):
    `{"robots": {"mir-1": {"actions": [...], "battery_min": 30}},
      "auto_charge": {"battery_floor": 20}, "assigner": {"prefer_not_charging": true}}`
    """
    out: list[tuple[tuple[str, ...], object]] = []
    for serial, r in (edits.get("robots") or {}).items():
        for key, value in (r or {}).items():
            if key not in ROBOT_KEYS:
                raise EditError(f"robots.{serial}.{key} no se puede editar desde aquí")
            if key == "actions":
                if not isinstance(value, list) or not all(isinstance(a, str) and a for a in value):
                    raise EditError(f"robots.{serial}.actions debe ser una lista de nombres")
                out.append((("robots", serial, "actions"), list(value)))
            else:
                out.append((("robots", serial, key), _number(f"robots.{serial}.{key}", value, float)))
    for block in ("auto_charge", "assigner"):
        for key, value in (edits.get(block) or {}).items():
            typ = GLOBAL_KEYS.get((block, key))
            if typ is None:
                raise EditError(f"{block}.{key} no se puede editar desde aquí")
            if typ is bool:
                if not isinstance(value, bool):
                    raise EditError(f"{block}.{key} debe ser true o false")
                out.append(((block, key), value))
            else:
                out.append(((block, key), _number(f"{block}.{key}", value, typ)))
    unknown = set(edits) - {"robots", "auto_charge", "assigner"}
    if unknown:
        raise EditError(f"no se puede editar {sorted(unknown)} desde aquí")
    return out


def _number(where: str, value, typ: type):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise EditError(f"{where} debe ser un número")
    if typ is int and value != int(value):
        raise EditError(f"{where} debe ser un número entero")
    if value < 0:
        raise EditError(f"{where} no puede ser negativo")
    return int(value) if value == int(value) else float(value)   # 25.0 → 25 en el fichero


def _yaml_value(value) -> str:
    """Valor → texto YAML de una línea: `[a, b]`, `25`, `true`."""
    text = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10**6)
    return text.removesuffix("\n...\n").strip()


def _find(node: yaml.MappingNode, key: str):
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return k, v
    return None


def _apply_one(text: str, path: tuple[str, ...], value) -> str:
    """Pone `value` en la ruta `path` tocando solo ese trozo del texto."""
    root = yaml.compose(text)
    if not isinstance(root, yaml.MappingNode):
        raise EditError("fleet.yaml no es un mapping")
    node, where = root, ""
    for i, key in enumerate(path):
        found = _find(node, key)
        last = i == len(path) - 1
        if found is None:
            return _insert(text, node, path[i:], value, where)
        _, child = found
        where = f"{where}.{key}" if where else key
        if last:
            if isinstance(child, yaml.MappingNode):
                raise EditError(f"{where} es un bloque, no un valor")
            a, b = child.start_mark.index, child.end_mark.index
            # `clave:` sin valor (null implícito): el nodo está vacío justo tras los dos puntos.
            pad = " " if a == b and text[a - 1:a] == ":" else ""
            new = pad + _yaml_value(value)
            return text[:a] + new + _keep_comment_column(text[b:], child.end_mark.column, a, new, text)
        if not isinstance(child, yaml.MappingNode):
            raise EditError(f"{where} no es un bloque: edítalo a mano")
        node = child
    raise AssertionError("ruta vacía")


def _keep_comment_column(rest: str, old_end_col: int, a: int, new: str, text: str) -> str:
    """Si tras el valor viene un comentario (`25            # ...`), ajusta los
    espacios para que el `#` siga en su columna; si el valor nuevo no cabe,
    deja dos espacios (o uno, si el original solo tenía uno)."""
    m = re.match(r"( +)#", rest)
    if not m:
        return rest
    comment_col = old_end_col + len(m.group(1))
    new_end_col = a - (text.rfind("\n", 0, a) + 1) + len(new)
    least = min(2, len(m.group(1)))
    return " " * max(least, comment_col - new_end_col) + rest[len(m.group(1)):]


def _insert(text: str, parent: yaml.MappingNode, rest: tuple[str, ...], value, where: str) -> str:
    """La clave no existe: se añade como primera clave de su bloque (con la
    sangría de sus hermanas) o, si falta el bloque de primer nivel, al final."""
    if len(rest) == 2 and not where and rest[0] in ("auto_charge", "assigner"):
        # Falta el bloque de primer nivel entero: se añade al final del fichero.
        sep = "" if text.endswith("\n") else "\n"
        return f"{text}{sep}\n{rest[0]}:\n  {rest[1]}: {_yaml_value(value)}\n"
    if len(rest) != 1:
        raise EditError(f"{where + '.' if where else ''}{rest[0]} no existe en fleet.yaml")
    if parent.flow_style or not parent.value:
        raise EditError(f"{where} tiene una forma que no se sabe editar sin estropearla: edítalo a mano")
    first_key = parent.value[0][0]
    line_start = text.rfind("\n", 0, first_key.start_mark.index) + 1
    indent = " " * first_key.start_mark.column
    return text[:line_start] + f"{indent}{rest[0]}: {_yaml_value(value)}\n" + text[line_start:]


def apply_edits(text: str, edits: dict) -> str:
    """Texto de fleet.yaml + ediciones → texto nuevo. Cada edición vuelve a
    componer el YAML: las posiciones cambian al sustituir un valor."""
    for path, value in normalize(edits):
        if path[0] == "robots" and _find_robot(text, path[1]) is None:
            raise EditError(f"el robot '{path[1]}' no está en fleet.yaml")
        text = _apply_one(text, path, value)
    # Comprobación final: lo que se lee del texto nuevo es lo que se pidió.
    data = yaml.safe_load(text)
    for path, value in normalize(edits):
        got = data
        for key in path:
            got = got[key]
        if got != value:
            raise EditError(f"no se pudo escribir {'.'.join(path)} (se leería {got!r})")
    return text


def _find_robot(text: str, serial: str):
    root = yaml.compose(text)
    robots = _find(root, "robots")
    return _find(robots[1], serial) if robots and isinstance(robots[1], yaml.MappingNode) else None


# ------------------------------------------------------------------ guardar
def validate(path: Path, env_path=None) -> FleetConfig:
    """Mismo cargador que el arranque + el driver de cada robot (sus claves
    las valida él). Lanza `ConfigError`."""
    cfg = load_config(path, env_path)
    for rc in cfg.robots.values():
        make_driver(rc, cfg)        # sin red: solo construye y valida
    return cfg


def save(path: str | Path, edits: dict, env_path=None, now: datetime | None = None) -> SaveResult:
    """Aplica `edits` a `path` si la config resultante es válida. Lanza
    `EditError`/`ConfigError` sin tocar el fichero si algo falla."""
    path = Path(path)
    old = path.read_text(encoding="utf-8")
    new = apply_edits(old, edits)
    if new == old:
        return SaveResult(validate(path, env_path), changed=False, backup=None)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    try:
        cfg = validate(tmp, env_path)
    except ConfigError:
        tmp.unlink(missing_ok=True)
        raise
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    shutil.copy2(path, backup)
    os.replace(tmp, path)            # atómico: nunca queda un fleet.yaml a medias
    return SaveResult(cfg, changed=True, backup=backup)
