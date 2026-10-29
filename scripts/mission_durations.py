#!/usr/bin/env python3
"""Cuánto tardan de verdad las actions de un robot, y qué `expected_s` poner.

Uso: mission_durations.py <serial> [--last N]

Solo lectura. Lee las N últimas entradas terminadas de la cola del MiR (300
por defecto; la cola guarda toda la historia, también lo lanzado desde la
web o la tablet), se queda con las de las missions de las actions del robot
y, por action, da cuántas terminaron bien, la mediana, el p90 y el máximo
del tiempo en ejecución (`started` → `finished`, sin la espera en cola).

Propuesta de `expected_s` (decisión 123): p90 × 1,5, redondeado hacia
arriba a 10 s, y solo con al menos 5 ejecuciones terminadas bien. Con menos
no hay de dónde sacar lo "normal": se pone a mano y se revisa más adelante.
Lo propuesto se copia al Configurador (o a `fleet.yaml`); el script no
escribe nada.
"""
from __future__ import annotations

import math
import statistics
import sys
from datetime import datetime

from _common import client_for, load, robot_or_exit, setup_logging

FINAL = {"Done", "Aborted", "Cancelled", "Canceled"}
MIN_SAMPLES = 5        # por debajo, la propuesta sería ruido
MARGIN = 1.5           # sobre el p90: que avise lo raro, no lo algo lento


def run_seconds(entry: dict) -> float | None:
    """`GET /mission_queue/<id>` → segundos de `started` a `finished` (None si falta alguno)."""
    try:
        start = datetime.fromisoformat(str(entry["started"]))
        end = datetime.fromisoformat(str(entry["finished"]))
    except (KeyError, TypeError, ValueError):
        return None
    return (end - start).total_seconds()


def percentile(values: list[float], p: float) -> float:
    """Percentil con interpolación lineal (el de una hoja de cálculo)."""
    v = sorted(values)
    k = (len(v) - 1) * p
    i = int(k)
    return v[i] + (v[min(i + 1, len(v) - 1)] - v[i]) * (k - i)


def propose(done_s: list[float]) -> int | None:
    """`expected_s` propuesto, o None si hay menos de MIN_SAMPLES."""
    if len(done_s) < MIN_SAMPLES:
        return None
    return int(math.ceil(percentile(done_s, 0.9) * MARGIN / 10) * 10)


def main() -> None:
    setup_logging()
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print(__doc__); sys.exit(2)
    serial = args[0]
    last = int(args[args.index("--last") + 1]) if "--last" in args else 300
    cfg = load()
    robot = robot_or_exit(cfg, serial)
    current = cfg.robots[serial].expected_s
    client = client_for(robot)

    entries = sorted(client.mission_queue_get(), key=lambda e: int(e["id"]))
    finished = [e for e in entries if e.get("state") in FINAL][-last:]
    print(f"== [{serial}] {len(entries)} entradas en la cola; se leen las {len(finished)} últimas terminadas")

    names: dict[str, str] = {}          # GUID → nombre en la web, una petición por mission
    by_mission: dict[str, list[dict]] = {}
    for e in finished:
        d = client.mission_queue_id_get(int(e["id"]))
        guid = str(d.get("mission_id", ""))
        if guid not in names:
            try:
                names[guid] = str(client.missions_mission_id_get(guid).get("name") or "")
            except Exception:
                names[guid] = ""        # mission borrada del robot
        by_mission.setdefault(guid, []).append(d)

    print(f"\n{'action':22} {'bien':>4} {'mal':>4} {'mediana':>8} {'p90':>6} {'máx.':>6} {'actual':>7} {'propuesta':>9}")
    for atype, a in sorted(robot.actions.items()):
        # `mission` es el GUID del catálogo o el nombre en la web (config/fleet.yaml).
        runs = [d for g, ds in by_mission.items() if a.mission in (g, names[g]) for d in ds]
        ok = [s for s in (run_seconds(d) for d in runs if d.get("state") == "Done") if s is not None]
        bad = sum(1 for d in runs if d.get("state") != "Done")
        now = f"{current[atype]:.0f}" if atype in current else "—"
        if not ok:
            print(f"{atype:22} {0:4} {bad:4} {'':>8} {'':>6} {'':>6} {now:>7}  sin ejecuciones terminadas bien")
            continue
        p = propose(ok)
        print(f"{atype:22} {len(ok):4} {bad:4} {statistics.median(ok):7.0f}s {percentile(ok, 0.9):5.0f}s "
              f"{max(ok):5.0f}s {now:>7} {p if p is not None else f'(<{MIN_SAMPLES})':>9}")
    print(f"\nPropuesta = p90 × {MARGIN}, redondeado a 10 s, con ≥ {MIN_SAMPLES} ejecuciones terminadas bien. "
          "Las horas son de la cola del robot (lo lanzado desde la web también cuenta).")


if __name__ == "__main__":
    main()
