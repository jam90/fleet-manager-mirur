#!/usr/bin/env python3
"""Encola una mission del MiR por NOMBRE (del grupo de fleet.yaml) y sigue su
estado en `mission_queue` hasta que termina. Prueba directa REST, sin MQTT.

Uso: run_mission.py <serial> <nombre_mission> [key=value ...] [--priority N] [--no-wait]

¡El robot se mueve! Pedir permiso antes de usarlo en clase.
"""
from __future__ import annotations

import sys
import time

from _common import client_for, load, robot_or_exit, setup_logging
from fm.adapters.mir.client import QUEUE_ALIVE  # noqa: E402


def main() -> None:
    setup_logging()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__); sys.exit(2)
    serial, name, *kv = args
    priority = int(sys.argv[sys.argv.index("--priority") + 1]) if "--priority" in sys.argv else 0
    wait = "--no-wait" not in sys.argv

    cfg = load()
    robot = robot_or_exit(cfg, serial)
    client = client_for(robot)
    missions = client.index_missions_by_name(robot.mission_group)
    if name not in missions:
        print(f"[{serial}] mission '{name}' no está en el grupo '{robot.mission_group}'; hay: {sorted(missions)}")
        sys.exit(1)
    guid = missions[name]

    # Parámetros: key=value; `id` = input_name de Blockly (§9.1). Los valores
    # numéricos se envían como número.
    params = []
    for item in kv:
        k, v = item.split("=", 1)
        try:
            v = float(v) if "." in v else int(v)
        except ValueError:
            pass
        params.append({"id": k, "value": v})
    exposed = client.index_mission_params(guid)
    unknown = {p["id"] for p in params} - exposed
    if unknown:
        print(f"[{serial}] aviso: la mission no expone los inputs {sorted(unknown)} (expone {sorted(exposed)})")

    st = client.status_get()
    print(f"[{serial}] antes: state={st.state_id} ({st.state_text}) bat={st.battery_percentage:.1f}% "
          f"pos=({st.x:.2f},{st.y:.2f}) queue_id={st.mission_queue_id}")
    q = client.mission_queue_post(guid, params or None, priority)
    qid = q["id"]
    print(f"[{serial}] POST mission_queue '{name}' ({guid}) → id={qid} state={q.get('state')}")
    if not wait:
        return

    last = None
    t0 = time.monotonic()
    while True:
        q = client.mission_queue_id_get(qid)
        st = client.status_get()
        line = (f"[{serial}] t={time.monotonic() - t0:5.1f}s queue={q['state']:<9} "
                f"robot={st.state_text:<9} pos=({st.x:.2f},{st.y:.2f}) '{st.mission_text}'")
        if line[:60] != (last or "")[:60] or int(time.monotonic() - t0) % 5 == 0:
            print(line)
        last = line
        if q["state"] not in QUEUE_ALIVE:
            print(f"[{serial}] fin: {q['state']} (finished={q.get('finished')})")
            break
        time.sleep(1)


if __name__ == "__main__":
    main()
