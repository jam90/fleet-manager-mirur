#!/usr/bin/env python3
"""Publica una order en vda5050/v3/MiR/fleet/order y espera el order_response.

Uso: send_fleet_order.py <actionType> [key=value ...] [--order-id X] [--update-id N]
"""
from __future__ import annotations

import sys

from _common import load, setup_logging
from _mqtt import build_order, connect, parse_kv, publish, wait_for
from fm.vda5050.header import topic


def main() -> None:
    setup_logging()
    argv = sys.argv[1:]
    order_id = argv[argv.index("--order-id") + 1] if "--order-id" in argv else None
    update_id = int(argv[argv.index("--update-id") + 1]) if "--update-id" in argv else 0
    pos = [a for i, a in enumerate(argv) if not a.startswith("--") and not (i > 0 and argv[i - 1].startswith("--"))]
    if len(pos) < 1:
        print(__doc__); sys.exit(2)
    action_type, *kv = pos
    cfg = load()
    man = cfg.mqtt.manufacturer
    order = build_order(man, "fleet", action_type, parse_kv(kv), order_id or f"fleet-{__import__('uuid').uuid4().hex[:8]}", update_id)

    c = connect(cfg)
    c.subscribe(topic(man, "fleet", "order_response"), 1)
    publish(c, topic(man, "fleet", "order"), order)
    print(f"[fleet] order {order['orderId']}/{order['orderUpdateId']} {action_type} publicada")
    resp = wait_for(c, topic(man, "fleet", "order_response"),
                    lambda d: d.get("orderId") == order["orderId"], 10)
    if resp is None:
        print("sin order_response en 10 s (¿FM arrancado?)")
    elif resp["status"] == "ASSIGNED":
        print(f"[fleet] ASSIGNED → {resp['assignedSerial']}: {resp.get('description')}")
    else:
        print(f"[fleet] REJECTED {resp.get('errorType')}: {resp.get('errorDescription')}")
    c.loop_stop()


if __name__ == "__main__":
    main()
