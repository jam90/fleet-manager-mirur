"""Edición de `fleet.yaml` desde el configurador (docs/plan-admin.md §2.3).

`fleet.yaml` está comentado a propósito (es material didáctico), así que no
se reescribe entero: se cambian en el texto **solo las claves editadas**,
usando la posición exacta que da el compositor de PyYAML (`yaml.compose` →
`start_mark`/`end_mark` de cada nodo). Comentarios, orden, espacios y comillas
del resto del fichero quedan intactos, y el diff en git es justo lo cambiado
(decisión 96: se probó `ruamel.yaml` y normalizaba `{ a: 1 }` → `{a: 1}` y
juntaba listas partidas en varias líneas).

Tres operaciones sobre el texto, todas a partir de una ruta de claves:
- **poner** un valor: si la clave existe se sustituye su valor (escalar o
  flujo `[...]`/`{...}` en su sitio; un bloque, sus líneas); si no, se añade
  como última clave de su bloque, con la sangría de sus hermanas;
- **quitar** una clave: se borran sus líneas;
- **comentario** de una línea (`mission: <GUID>   # <nombre>`), opcional.

Qué se puede editar (decisión 93): v1, las actions habilitadas y
`battery_min` de cada robot, `auto_charge.*` y `assigner.prefer_not_charging`;
v2, el catálogo `actions:` (crear, cambiar y borrar definiciones). El
contenido de una definición es del driver: aquí no se interpreta, lo valida
el driver al guardar.

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

# Claves editables fuera de `robots:` y `actions:`: (bloque, clave) → tipo.
GLOBAL_KEYS: dict[tuple[str, str], type] = {
    ("auto_charge", "battery_floor"): float,
    ("auto_charge", "priority"): int,
    ("auto_charge", "abort_cooldown_s"): float,
    ("assigner", "prefer_not_charging"): bool,
}
ROBOT_KEYS = ("actions", "battery_min")
# Nombre de action (= actionType VDA): que se pueda escribir en YAML sin comillas.
ACTION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
COMMENT_GAP = "   "          # espacios antes de un comentario nuevo, como en fleet.yaml


class EditError(ValueError):
    """Edición no aplicable (clave no editable, valor de tipo incorrecto,
    fichero con una forma que no se sabe editar sin estropearlo)."""


@dataclass
class SaveResult:
    cfg: FleetConfig            # la config nueva, ya validada
    changed: bool               # False = las ediciones no cambiaban nada
    backup: Path | None         # copia del fichero anterior


@dataclass
class Op:
    """Una operación sobre el texto: poner `value` en `path` (con comentario
    opcional para esa línea) o, con `delete`, quitar la clave."""
    path: tuple[str, ...]
    value: object = None
    comment: str | None = None
    delete: bool = False


# --------------------------------------------------------------- ediciones
def plan(text: str, edits: dict) -> list[Op]:
    """Ediciones de la UI → operaciones sobre el texto, ya tipadas. Las del
    catálogo se calculan comparando con lo que hay: solo cambian las claves
    distintas, para conservar los comentarios de las demás.

    Formato de entrada (todo opcional):
    `{"actions": {"coger": {"definition": {...}, "comments": {"mission": "coger"}},
                  "vieja": None},                                   # None = borrar
      "robots": {"mir-1": {"actions": [...], "battery_min": 30}},
      "auto_charge": {"battery_floor": 20}, "assigner": {"prefer_not_charging": true}}`
    """
    unknown = set(edits) - {"actions", "robots", "auto_charge", "assigner"}
    if unknown:
        raise EditError(f"no se puede editar {sorted(unknown)} desde aquí")
    current = yaml.safe_load(text) or {}
    ops: list[Op] = []
    # El catálogo primero: un robot puede habilitar en la misma edición una action nueva.
    catalog = current.get("actions") or {}
    for name, change in (edits.get("actions") or {}).items():
        if not isinstance(name, str) or not ACTION_NAME.match(name):
            raise EditError(f"'{name}' no vale como nombre de action: letras, números, '_', '-' o '.', "
                            "empezando por letra")
        if change is None:
            if name in catalog:
                ops.append(Op(("actions", name), delete=True))
            continue
        definition, comments = _definition(name, change)
        ops += _diff_action(name, catalog.get(name, ...), definition, comments)
    for serial, r in (edits.get("robots") or {}).items():
        for key, value in (r or {}).items():
            if key not in ROBOT_KEYS:
                raise EditError(f"robots.{serial}.{key} no se puede editar desde aquí")
            if key == "actions":
                if not isinstance(value, list) or not all(isinstance(a, str) and a for a in value):
                    raise EditError(f"robots.{serial}.actions debe ser una lista de nombres")
                ops.append(Op(("robots", serial, "actions"), list(value)))
            else:
                ops.append(Op(("robots", serial, key), _number(f"robots.{serial}.{key}", value, float)))
    for block in ("auto_charge", "assigner"):
        for key, value in (edits.get(block) or {}).items():
            typ = GLOBAL_KEYS.get((block, key))
            if typ is None:
                raise EditError(f"{block}.{key} no se puede editar desde aquí")
            if typ is bool:
                if not isinstance(value, bool):
                    raise EditError(f"{block}.{key} debe ser true o false")
                ops.append(Op((block, key), value))
            else:
                ops.append(Op((block, key), _number(f"{block}.{key}", value, typ)))
    return ops


def _definition(name: str, change) -> tuple[dict, dict]:
    if not isinstance(change, dict) or not isinstance(change.get("definition", {}), dict):
        raise EditError(f"actions.{name}: se espera {{definition: {{...}}, comments: {{...}}}}")
    definition = change.get("definition") or {}
    comments = change.get("comments") or {}
    if not all(isinstance(k, str) and k for k in definition):
        raise EditError(f"actions.{name}: las claves de la definición deben ser texto")
    if not all(isinstance(v, str) and "\n" not in v for v in comments.values()):
        raise EditError(f"actions.{name}: los comentarios deben ser texto de una línea")
    return definition, comments


def _diff_action(name: str, old, new: dict, comments: dict) -> list[Op]:
    """Operaciones para que `actions.<name>` pase de `old` a `new` (`...` =
    no existe). Si antes era un mapping, clave a clave; si no (no existía,
    vacía o escrita en flujo), la definición entera."""
    if not isinstance(old, dict) or not old:
        if old is not ... and (old or {}) == new:
            return []
        # Entera; y luego, clave a clave, los comentarios que se piden.
        return [Op(("actions", name), new),
                *[Op(("actions", name, k), new[k], comment=c) for k, c in comments.items() if k in new]]
    ops = [Op(("actions", name, k), delete=True) for k in old if k not in new]
    for k, v in new.items():
        if old.get(k, ...) != v:
            ops.append(Op(("actions", name, k), v, comment=comments.get(k)))
    return ops


def _number(where: str, value, typ: type):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise EditError(f"{where} debe ser un número")
    if typ is int and value != int(value):
        raise EditError(f"{where} debe ser un número entero")
    if value < 0:
        raise EditError(f"{where} no puede ser negativo")
    return int(value) if value == int(value) else float(value)   # 25.0 → 25 en el fichero


# ------------------------------------------------------- texto y posiciones
def _flow(value) -> str:
    """Valor → texto YAML de una línea: `[a, b]`, `25`, `true`, `{min: 1}`."""
    text = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10**6, sort_keys=False)
    return text.removesuffix("\n...\n").strip()


def _render(key: str, value, indent: int, depth: int, comment: str | None = None) -> str:
    """`clave: valor` como líneas de bloque. Los mappings se escriben en
    bloque hasta `depth` niveles (definición de action → `number_inputs`) y
    en flujo a partir de ahí (`{min: 1, max: 10, step: 1}`), como en fleet.yaml."""
    pad = " " * indent
    if isinstance(value, dict) and value and depth > 0:
        return f"{pad}{key}:\n" + "".join(_render(k, v, indent + 2, depth - 1) for k, v in value.items())
    tail = f"{COMMENT_GAP}# {comment}" if comment else ""
    return f"{pad}{key}: {_flow(value)}{tail}\n"


def _block_depth(path: tuple[str, ...]) -> int:
    """Cuántos niveles en bloque lleva lo que se escribe en `path`."""
    if path[0] == "actions":
        return max(0, 4 - len(path))     # action entera: 2; una de sus claves: 1
    return 0


def _find(node, key: str):
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if isinstance(k, yaml.ScalarNode) and k.value == key:
                return k, v
    return None


def _line_start(text: str, i: int) -> int:
    return text.rfind("\n", 0, i) + 1


def _line_end(text: str, i: int) -> int:
    """Índice del `\\n` que cierra la línea de `i` (o el final del texto)."""
    j = text.find("\n", i)
    return len(text) if j < 0 else j


def _last_index(node) -> int:
    """Dónde acaba de verdad un nodo: el `end_mark` de un bloque apunta al
    siguiente token (tras líneas en blanco y comentarios), así que se baja
    hasta su última hoja."""
    if isinstance(node, yaml.MappingNode) and not node.flow_style and node.value:
        k, v = node.value[-1]
        return max(_last_index(v), k.end_mark.index)
    if isinstance(node, yaml.SequenceNode) and not node.flow_style and node.value:
        return _last_index(node.value[-1])
    return node.end_mark.index


def _is_block(node) -> bool:
    return isinstance(node, (yaml.MappingNode, yaml.SequenceNode)) and not node.flow_style and bool(node.value)


def _with_comment(text: str, i: int, comment: str) -> str:
    """Pone `# comment` en la línea de la posición `i` (un valor recién
    escrito): sustituye el comentario que haya detrás o lo añade."""
    end = _line_end(text, i)
    m = re.search(r"(\s+)#.*$", text[i:end])
    if m:
        return text[:i + m.start()] + f"{m.group(1)}# {comment}" + text[end:]
    return text[:end] + f"{COMMENT_GAP}# {comment}" + text[end:]


def _keep_comment_column(rest: str, old_end_col: int, a: int, new: str, text: str) -> str:
    """Si tras el valor viene un comentario (`25            # ...`), ajusta los
    espacios para que el `#` siga en su columna; si el valor nuevo no cabe,
    deja dos espacios (o uno, si el original solo tenía uno)."""
    m = re.match(r"( +)#", rest)
    if not m:
        return rest
    comment_col = old_end_col + len(m.group(1))
    new_end_col = a - _line_start(text, a) + len(new)
    least = min(2, len(m.group(1)))
    return " " * max(least, comment_col - new_end_col) + rest[len(m.group(1)):]


def _set(text: str, op: Op) -> str:
    root = yaml.compose(text)
    if not isinstance(root, yaml.MappingNode):
        raise EditError("fleet.yaml no es un mapping")
    node, where = root, ""
    depth = _block_depth(op.path)
    for i, key in enumerate(op.path):
        found = _find(node, key)
        if found is None:
            return _insert(text, node, op.path[i:], op, where, depth)
        k, child = found
        where = f"{where}.{key}" if where else key
        if i < len(op.path) - 1:
            if not isinstance(child, yaml.MappingNode):
                raise EditError(f"{where} no es un bloque: edítalo a mano")
            node = child
            continue
        # Clave encontrada: sustituir su valor.
        if _is_block(child) or (isinstance(op.value, dict) and op.value and depth > 0):
            # En bloque: se reescriben las líneas de la clave (la de la clave
            # conserva su comentario si lo tenía).
            a = _line_start(text, k.start_mark.index)
            b = _line_end(text, _last_index(child))
            key_line_end = _line_end(text, k.start_mark.index)
            m = re.search(r"\s+#.*$", text[k.end_mark.index:key_line_end])
            new = _render(key, op.value, k.start_mark.column, depth, op.comment)
            if m and isinstance(op.value, dict) and op.value and depth > 0:
                first, rest = new.split("\n", 1)
                new = first + m.group(0) + "\n" + rest
            return text[:a] + new.removesuffix("\n") + text[b:]
        a, b = child.start_mark.index, child.end_mark.index
        # `clave:` sin valor (null implícito): el nodo está vacío justo tras los dos puntos.
        new = (" " if a == b and text[a - 1:a] == ":" else "") + _flow(op.value)
        text = text[:a] + new + _keep_comment_column(text[b:], child.end_mark.column, a, new, text)
        return _with_comment(text, a, op.comment) if op.comment else text
    raise AssertionError("ruta vacía")


def _insert(text: str, parent, rest: tuple[str, ...], op: Op, where: str, depth: int) -> str:
    """La clave no existe. Se añade como última clave de su bloque, con la
    sangría de sus hermanas; si falta un bloque de primer nivel, al final."""
    if len(rest) == 2 and not where and rest[0] in ("auto_charge", "assigner", "actions"):
        sep = "" if text.endswith("\n") else "\n"
        return f"{text}{sep}\n{rest[0]}:\n" + _render(rest[1], op.value, 2, depth, op.comment)
    if len(rest) != 1:
        raise EditError(f"{where + '.' if where else ''}{rest[0]} no existe en fleet.yaml")
    if not isinstance(parent, yaml.MappingNode) or parent.flow_style or not parent.value:
        raise EditError(f"{where} tiene una forma que no se sabe editar sin estropearla: edítalo a mano")
    col = parent.value[0][0].start_mark.column
    at = _line_end(text, _last_index(parent))
    block = _render(rest[0], op.value, col, depth, op.comment)
    return text[:at] + "\n" + block.removesuffix("\n") + text[at:]


def _delete(text: str, op: Op) -> str:
    root = yaml.compose(text)
    node = root
    for key in op.path[:-1]:
        found = _find(node, key)
        if found is None:
            return text
        node = found[1]
    found = _find(node, op.path[-1])
    if found is None:
        return text                       # ya no estaba
    k, v = found
    a = _line_start(text, k.start_mark.index)
    if text[a:k.start_mark.index].strip():
        raise EditError(f"{'.'.join(op.path)} comparte línea con otra clave: edítalo a mano")
    b = _line_end(text, _last_index(v))
    return text[:a] + text[b + 1:]


def apply_edits(text: str, edits: dict) -> str:
    """Texto de fleet.yaml + ediciones → texto nuevo. Cada operación vuelve a
    componer el YAML: las posiciones cambian al tocar el texto."""
    ops = plan(text, edits)
    for op in ops:
        if op.path[0] == "robots" and _find_robot(text, op.path[1]) is None:
            raise EditError(f"el robot '{op.path[1]}' no está en fleet.yaml")
        text = _delete(text, op) if op.delete else _set(text, op)
    # Comprobación final: lo que se lee del texto nuevo es lo que se pidió.
    data = yaml.safe_load(text) or {}
    for op in ops:
        parent = data
        for key in op.path[:-1]:
            parent = (parent or {}).get(key) if isinstance(parent, dict) else None
        got = parent.get(op.path[-1], ...) if isinstance(parent, dict) else ...
        want = ... if op.delete else op.value
        if not op.delete and op.path[0] == "actions" and len(op.path) == 2:
            got, want = (got or {}) if got is not ... else got, want or {}
        if got != want:
            raise EditError(f"no se pudo escribir {'.'.join(op.path)} (se leería {got!r})")
    return text


def _find_robot(text: str, serial: str):
    robots = _find(yaml.compose(text), "robots")
    return _find(robots[1], serial) if robots else None


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
