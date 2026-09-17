#!/usr/bin/env python3
"""GET /status crudo + resumen. Uso: read_status.py <serial> [--raw]"""
from __future__ import annotations

import json
import sys

from _common import client_for, load, robot_or_exit, setup_logging
from fm.adapters.mir.client import MirStatus


def main() -> None:
    setup_logging()
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    serial = sys.argv[1]
    raw = "--raw" in sys.argv
    cfg = load()
    client = client_for(robot_or_exit(cfg, serial))

    d = client.status_get_raw()
    if raw:
        print(json.dumps(d, indent=2, ensure_ascii=False))
    st = MirStatus.from_json(d)
    print(f"[{serial}] state={st.state_id} ({st.state_text}) mode={st.mode_id} ({st.mode_text})")
    print(f"[{serial}] bat={st.battery_percentage:.1f}% time_remaining={st.battery_time_remaining}")
    print(f"[{serial}] pos=({st.x:.2f},{st.y:.2f}) theta={st.theta:.3f} rad map={st.map_id}")
    print(f"[{serial}] mission_text='{st.mission_text}' queue_id={st.mission_queue_id} errors={len(st.errors)}")
    for e in st.errors:
        print(f"[{serial}]   error {e.get('code')}: {e.get('description')}")
    # Campos que podrían delatar la carga (decisión pendiente, CLAUDE.md §5.2).
    extra = {k: d.get(k) for k in ("battery_time_remaining", "mode_id", "mode_text",
                                   "mission_queue_url", "unloaded_map_changes") if k in d}
    print(f"[{serial}] campos candidatos a 'charging': {extra}")


if __name__ == "__main__":
    main()
