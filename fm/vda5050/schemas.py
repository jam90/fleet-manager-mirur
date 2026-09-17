"""Validación contra los JSON Schema oficiales de `schemas/` (draft 2020-12).

Se usa en tests (todo lo que publica el FM debe validar) y en runtime con
`--validate` (lo que entra por `order`/`instantActions`).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMAS_DIR = Path(__file__).resolve().parent.parent.parent / "schemas"


@lru_cache(maxsize=None)
def _validator(subtopic: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS_DIR / f"{subtopic}.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def validation_errors(subtopic: str, payload: object) -> list[str]:
    """Lista de fallos legibles (vacía si valida). `subtopic` = state|order|…"""
    out = []
    for e in sorted(_validator(subtopic).iter_errors(payload), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in e.path) or "<raíz>"
        out.append(f"{where}: {e.message}")
    return out


def assert_valid(subtopic: str, payload: object) -> None:
    errs = validation_errors(subtopic, payload)
    if errs:
        raise AssertionError(f"{subtopic} no valida:\n  " + "\n  ".join(errs))
