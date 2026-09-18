#!/usr/bin/env python3
"""Publica una `instantActions` a UN robot: vda5050/v3/<manufacturer>/<serial>/instantActions.

Uso: send_instant_action.py <serial> <actionType> [<actionType> ...]
     actionType: startPause | stopPause | cancelOrder | stateRequest | factsheetRequest
Luego muestra `instantActionStates` y `errors` del siguiente `state`.
"""
from __future__ import annotations

import sys
import uuid

from _common import load, manufacturer_for, setup_logging
from _mqtt import _counter, connect, publish, wait_for
from fm.vda5050.header import make_header, topic


def main() -> None:
    setup_logging()
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(2)
    serial, *types = sys.argv[1:]
    cfg = load()
    man = manufacturer_for(cfg, serial)
    ids = [f"ia-{uuid.uuid4().hex[:6]}" for _ in types]
    msg = {**make_header(_counter, man, serial, "instantActions"),
           "actions": [{"actionType": t, "actionId": i, "blockingType": "NONE", "actionParameters": []}
                       for t, i in zip(types, ids)]}
    c = connect(cfg)
    publish(c, topic(man, serial, "instantActions"), msg)
    print(f"[{serial}] instantActions {types} publicadas")
    st = wait_for(c, topic(man, serial, "state"),
                  lambda d: {s["actionId"] for s in d.get("instantActionStates", [])} >= set(ids), 10)
    if st is None:
        print("sin state con el resultado en 10 s (¿FM arrancado?)")
    else:
        for s in st["instantActionStates"]:
            print(f"[{serial}] {s['actionType']}: {s['actionStatus']}")
        for e in st.get("errors", []):
            print(f"   error {e['errorType']}: {e.get('errorDescription')}")
    c.loop_stop()


if __name__ == "__main__":
    main()
