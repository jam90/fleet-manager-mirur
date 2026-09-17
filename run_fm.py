#!/usr/bin/env python3
"""Fleet Manager MiR250 — punto de entrada.

    python run_fm.py [--period 1.0] [--robot mir-1 --robot mir-2] [--config config/fleet.yaml]

Bucle principal (un hilo): cada `period` segundos, por robot: GET /status →
`state` VDA → publicar. Los mensajes MQTT entrantes llegan por `bus.inbox`
y se drenan al principio de cada tick (H2+).
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fm.adapters.mir import to_vda_state  # noqa: E402
from fm.config import load_config  # noqa: E402
from fm.fleet import Dispatcher  # noqa: E402
from fm.mqtt_bus import MqttBus  # noqa: E402
from fm.robot import Robot  # noqa: E402

log = logging.getLogger("fm")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--period", type=float, default=1.0, help="segundos entre ticks (por defecto 1)")
    p.add_argument("--robot", action="append", help="limitar a estos serials (repetible)")
    p.add_argument("--config", default="config/fleet.yaml")
    p.add_argument("--env", default=".env")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s")
    cfg = load_config(args.config, args.env)
    serials = args.robot or sorted(cfg.robots)
    unknown = [s for s in serials if s not in cfg.robots]
    if unknown:
        log.error("robots desconocidos: %s (en fleet.yaml: %s)", unknown, sorted(cfg.robots))
        return 2
    robots = {s: Robot(cfg.robots[s]) for s in serials}

    bus = MqttBus(cfg.mqtt.host, cfg.mqtt.port, cfg.mqtt.manufacturer, serials)
    bus.start()

    stop = False

    def _on_signal(signum, frame):
        nonlocal stop
        log.info("[fm] señal %s: cerrando", signal.Signals(signum).name)
        stop = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    for r in robots.values():
        r.refresh_indices(cfg.auto_charge.mission, cfg.mission_group)
        r.poll()

    def publish_state(r: Robot) -> None:
        state = to_vda_state(bus.next_header(r.serial, "state"), r.last_status, r.orders.overlay(),
                             rest_error=r.last_error)
        bus.publish_raw(r.serial, "state", state.to_dict())

    dispatcher = Dispatcher(cfg, robots, bus, publish_state)
    dispatcher.subscribe()

    try:
        while not stop:
            t0 = time.monotonic()
            dispatcher.drain()
            for r in robots.values():
                tick_robot(r, bus, cfg)
            # Dormir lo que falte del periodo, en trozos cortos para reaccionar a señales.
            while not stop and (time.monotonic() - t0) < args.period:
                time.sleep(min(0.1, args.period))
    finally:
        bus.stop()
    return 0


def tick_robot(r: Robot, bus: MqttBus, cfg) -> None:
    st = r.poll()
    if st is not None and not r.indexed:
        r.refresh_indices(cfg.auto_charge.mission, cfg.mission_group)   # reintento tras un arranque sin red
    r.orders.poll(r.client)
    header = bus.next_header(r.serial, "state")
    state = to_vda_state(header, st, r.orders.overlay(), rest_error=r.last_error)
    bus.publish_raw(r.serial, "state", state.to_dict())
    if st is not None:
        acts = ",".join(f"{a.actionType}:{a.actionStatus}" for a in state.actionStates) or "-"
        r.log.info("state hdr=%d pos=(%.2f,%.2f) bat=%.1f%% state_id=%d order=%s/%d acts=%s errs=%d",
                   state.headerId, st.x, st.y, st.battery_percentage, st.state_id,
                   state.orderId or "-", state.orderUpdateId, acts, len(state.errors))
    else:
        r.log.info("state hdr=%d SIN /status errs=%d", state.headerId, len(state.errors))


if __name__ == "__main__":
    sys.exit(main())
