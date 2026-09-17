#!/usr/bin/env python3
"""Missions (nombre, GUID, input_names) y positions de un robot.

Uso: list_missions.py <serial> [--all]
Por defecto se listan solo las missions del `mission_group` de fleet.yaml;
con --all, todas las del robot. Los input_names se consultan únicamente para
las missions referenciadas en fleet.yaml (una petición por mission).
"""
from __future__ import annotations

import sys

from _common import client_for, load, robot_or_exit, setup_logging
from fm.mir_client import position_map_id  # noqa: E402  (_common ajusta sys.path)


def main() -> None:
    setup_logging()
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    serial = sys.argv[1]
    show_all = "--all" in sys.argv
    cfg = load()
    robot = robot_or_exit(cfg, serial)
    client = client_for(robot)

    wanted = {a.mission for a in robot.actions.values()}
    if cfg.auto_charge.mission:
        wanted.add(cfg.auto_charge.mission)

    if cfg.mission_group and "--all" not in sys.argv:
        groups = client.index_mission_groups_by_name()
        print(f"== [{serial}] mission_groups: {sorted(groups)}")
        if cfg.mission_group not in groups:
            print(f"!! el grupo '{cfg.mission_group}' NO existe en {serial}"); sys.exit(1)
        print(f"== [{serial}] missions del grupo '{cfg.mission_group}' ({groups[cfg.mission_group]}) ==")
        missions = client.mission_groups_group_id_missions_get(groups[cfg.mission_group])
    else:
        print(f"== [{serial}] missions (todas) ==")
        missions = client.missions_get()
    for m in sorted(missions, key=lambda m: m.get("name", "")):
        name, guid = m.get("name", ""), m.get("guid", "")
        mark = "*" if name in wanted else " "
        params = ""
        if name in wanted or (show_all and False):
            params = "  inputs=" + str(sorted(client.index_mission_params(guid)))
        print(f" {mark} {name!r:45} {guid}{params}")
    missing = wanted - {m.get("name") for m in missions}
    if missing:
        print(f"!! missions de fleet.yaml que NO existen en {serial}: {sorted(missing)}")

    st = client.status_get()
    print(f"\n== [{serial}] positions (mapa activo {st.map_id}) ==")
    for p in sorted(client.positions_get(), key=lambda p: p.get("name", "")):
        in_map = "" if position_map_id(p) == st.map_id else "  (otro mapa)"
        print(f"   {p.get('name')!r:35} type_id={p.get('type_id'):<3} {p.get('guid')}{in_map}")


if __name__ == "__main__":
    main()
