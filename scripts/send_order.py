#!/usr/bin/env python3
"""Publica una order VDA dirigida a UN robot: vda5050/v3/MiR/<serial>/order.

Uso: send_order.py <serial> <actionType> [key=value ...] [--order-id X] [--update-id N]
Luego observa el `state` del robot unos segundos.
"""
from __future__ import annotations

import sys

from _common import load, manufacturer_for, setup_logging
from _mqtt import build_order, connect, parse_kv, publish, wait_for
from fm.vda5050.header import topic


def main() -> None:
    setup_logging()
    argv = sys.argv[1:]
    order_id = argv[argv.index("--order-id") + 1] if "--order-id" in argv else None
    update_id = int(argv[argv.index("--update-id") + 1]) if "--update-id" in argv else 0
    pos = [a for i, a in enumerate(argv) if not a.startswith("--") and not (i > 0 and argv[i - 1].startswith("--"))]
    if len(pos) < 2:
        print(__doc__); sys.exit(2)
    serial, action_type, *kv = pos
    cfg = load()
    man = manufacturer_for(cfg, serial)
    order = build_order(man, serial, action_type, parse_kv(kv), order_id, update_id)

    c = connect(cfg)
    publish(c, topic(man, serial, "order"), order)
    print(f"[{serial}] order {order['orderId']}/{order['orderUpdateId']} {action_type} publicada")
    st = wait_for(c, topic(man, serial, "state"),
                  lambda d: d.get("orderId") == order["orderId"] or d.get("errors"), 10)
    if st is None:
        print("sin state del robot en 10 s (¿FM arrancado?)")
    else:
        print(f"[{serial}] state: order={st.get('orderId')}/{st.get('orderUpdateId')} "
              f"acts={[(a['actionType'], a['actionStatus']) for a in st.get('actionStates', [])]}")
        for e in st.get("errors", []):
            print(f"   error {e['errorType']}: {e.get('errorDescription')}")
    c.loop_stop()


if __name__ == "__main__":
    main()
