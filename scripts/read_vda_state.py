#!/usr/bin/env python3
"""`mosquitto_sub` en Python: resume cada `state`/`connection` en una línea.

Uso: read_vda_state.py [serial]   (sin serial: todos los robots)
Valida cada mensaje contra el schema oficial y avisa si no cumple.
"""
from __future__ import annotations

import json
import sys

import paho.mqtt.client as mqtt

from _common import load, setup_logging
from fm.vda5050.header import parse_topic, topic
from fm.vda5050.schemas import validation_errors


def summarize(pt, d: dict) -> str:
    if pt.subtopic == "connection":
        return f"[{pt.serial}] connection hdr={d['headerId']} {d['connectionState']}"
    pos = d.get("mobileRobotPosition")
    pos_s = f"({pos['x']:.2f},{pos['y']:.2f})" if pos else "(-)"
    ps = d.get("powerSupply", {})
    flags = ("D" if d.get("driving") else "-") + ("P" if d.get("paused") else "-") + \
            ("C" if ps.get("charging") else "-")
    return (f"[{pt.serial}] state hdr={d['headerId']} pos={pos_s} bat={ps.get('stateOfCharge', 0):.1f}% "
            f"{flags} {d.get('operatingMode')} order={d.get('orderId') or '-'}/{d.get('orderUpdateId')} "
            f"acts={len(d.get('actionStates', []))} errs={len(d.get('errors', []))}")


def main() -> None:
    setup_logging()
    cfg = load()
    serial = sys.argv[1] if len(sys.argv) > 1 else "+"
    man = cfg.mqtt.manufacturer

    def on_message(client, userdata, msg):
        pt = parse_topic(msg.topic)
        if pt is None:
            return
        try:
            d = json.loads(msg.payload)
        except ValueError:
            print(f"{msg.topic}: JSON inválido"); return
        print(summarize(pt, d))
        for e in validation_errors(pt.subtopic, d):
            print(f"   !! schema: {e}")
        for e in d.get("errors", []):
            print(f"   error {e['errorType']}/{e['errorLevel']}: {e.get('errorDescription', '')[:100]}")

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.on_message = on_message
    c.connect(cfg.mqtt.host, cfg.mqtt.port)
    c.subscribe(topic(man, serial, "state"))
    c.subscribe(topic(man, serial, "connection"))
    print(f"escuchando {topic(man, serial, 'state')} y connection… (Ctrl-C para salir)")
    try:
        c.loop_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
