"""Despliega una selección del catálogo (`catalog/`) en un MiR, con sus dependencias.

Uso:
    catalog_deploy.py <serial> <guid>… [--escribir]
    catalog_deploy.py <serial> --todo [--escribir]

Ejemplos:
    catalog_deploy.py mir-1 imperialfleet-mission000-01-0001-004
    catalog_deploy.py mir-1 --todo --escribir

Sin `--escribir` solo enseña la vista previa. La lógica está en
`fm/adapters/mir/sync.py` (la misma que usa la pestaña Sincronización de la
web): una mission arrastra su grupo, su session, sus positions, sus
sub-missions y sus sonidos; lo que el robot ya tiene igual no se toca, y lo
que tiene distinto se avisa y tampoco.
"""
from __future__ import annotations

import argparse
import sys

from _common import CATALOG, load, robot_or_exit

from fm.adapters.mir.sync import Catalog, MirSync

MARK = {"crear": "+", "corregir": "~", "igual": "=", "distinta": "!"}


def show(op) -> None:
    status = "" if op.status in ("pendiente", "sin cambios") else f"  [{op.status}]"
    print(f"{MARK[op.verb]} {op.kind:<10} {op.name!r:<40} {op.guid}  {op.detail}{status}")
    if op.warning:
        print(f"    ¡aviso! {op.warning}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("serial")
    ap.add_argument("guids", nargs="*")
    ap.add_argument("--todo", action="store_true", help="todo el catálogo")
    ap.add_argument("--escribir", action="store_true")
    a = ap.parse_args()
    if not a.guids and not a.todo:
        sys.exit("indica GUIDs del catálogo o --todo")

    r = robot_or_exit(load(), a.serial)
    sync = MirSync(a.serial, r.host, r.auth, CATALOG)
    selected = list(Catalog.load(CATALOG).items) if a.todo else a.guids
    if a.escribir:
        ops = sync.deploy(selected, progress=show)
    else:
        _, ops = sync.preview(selected)
        for op in ops:
            show(op)
    cambios = sum(op.verb in ("crear", "corregir") for op in ops)
    errores = sum(op.status in ("error", "saltada") for op in ops)
    print(f"\n{cambios} cambios" + (f", {errores} con error" if errores else "")
          + ("" if a.escribir else " (vista previa: añade --escribir)"))


if __name__ == "__main__":
    main()
