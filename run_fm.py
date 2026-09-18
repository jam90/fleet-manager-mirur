#!/usr/bin/env python3
"""Fleet Manager MiR250 — punto de entrada.

    python run_fm.py [--period 1.0] [--robot mir-1 --robot mir-2] [--config config/fleet.yaml]

Bucle principal: cada `period` segundos, `Robot.poll()` de todos los robots en
paralelo (solo red), y después, en el hilo principal, por robot: Telemetry →
`state` VDA → publicar. Los mensajes MQTT entrantes llegan por `bus.inbox`
y se drenan al principio de cada tick (H2+).
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fm.adapters import make_driver  # noqa: E402
from fm.config import ConfigError, load_config  # noqa: E402
from fm.fleet import Dispatcher  # noqa: E402
from fm.mqtt_bus import MqttBus  # noqa: E402
from fm.robot import Robot  # noqa: E402
from fm.vda5050.state_builder import to_vda_state  # noqa: E402

log = logging.getLogger("fm")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--period", type=float, default=1.0, help="segundos entre ticks (por defecto 1)")
    p.add_argument("--robot", action="append", help="limitar a estos serials (repetible)")
    p.add_argument("--config", default="config/fleet.yaml")
    p.add_argument("--env", default=".env")
    p.add_argument("--web-port", type=int, default=8050, help="puerto de la interfaz web (0 = sin web)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s")
    try:
        cfg = load_config(args.config, args.env)
    except ConfigError as e:
        log.error("configuración inválida: %s", e)
        return 2
    serials = args.robot or sorted(cfg.robots)
    unknown = [s for s in serials if s not in cfg.robots]
    if unknown:
        log.error("robots desconocidos: %s (en fleet.yaml: %s)", unknown, sorted(cfg.robots))
        return 2
    try:
        robots = {s: Robot(cfg.robots[s], make_driver(cfg.robots[s], cfg), cfg.auto_charge) for s in serials}
    except ConfigError as e:
        log.error("configuración inválida: %s", e)
        return 2

    bus = MqttBus(cfg.mqtt.host, cfg.mqtt.port, {s: robots[s].manufacturer for s in serials},
                  cfg.mqtt.fleet_manufacturer)
    web = None
    if args.web_port:
        from fm.web.server import WebServer   # import tardío: fastapi solo si se usa
        web = WebServer(cfg, robots, bus)     # instala el espejo antes de conectar al broker
    bus.start()

    stop = False

    def _on_signal(signum, frame):
        nonlocal stop
        log.info("[fm] señal %s: cerrando", signal.Signals(signum).name)
        stop = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # Un hilo por robot SOLO para la parte de red (`Robot.poll`). El tick
    # espera como mucho `grace` s: el poll de un robot lento (apagado → timeout
    # de 2 s) sigue en su hilo y ese robot publica su último estado conocido
    # hasta que vuelva. El resto del tick sigue en el hilo principal, sin
    # locks (decisión 42).
    pool = ThreadPoolExecutor(max_workers=max(1, len(robots)), thread_name_prefix="poll")
    pending: dict[str, Future] = {}
    grace = min(0.5, args.period / 2)

    def poll_all(timeout: float) -> None:
        for s, r in robots.items():
            if s not in pending:                 # el anterior aún no ha vuelto: no apilar
                pending[s] = pool.submit(r.poll)
        # Solo se espera a los robots que respondían: uno en backoff (apagado)
        # no retrasa el tick ni siquiera los `grace` s.
        awaited = [f for s, f in pending.items() if robots[s].poll_failures == 0]
        wait(awaited, timeout=timeout)
        for s in [s for s, f in pending.items() if f.done()]:
            exc = pending.pop(s).exception()
            if exc is not None:                  # un driver que lanza es un bug; no tumba el FM
                robots[s].log.error("poll() lanzó %s: %s", type(exc).__name__, exc)

    list(pool.map(lambda r: r.connect(), robots.values()))
    poll_all(timeout=10.0)

    def publish_state(r: Robot) -> None:
        state = to_vda_state(bus.next_header(r.serial, "state"), r.last_telemetry, r.overlay(),
                             error=r.last_error)
        bus.publish_raw(r.serial, "state", state.to_dict())

    dispatcher = Dispatcher(cfg, robots, bus, publish_state)
    dispatcher.subscribe()

    if web is not None:
        web.start(port=args.web_port)

    try:
        while not stop:
            t0 = time.monotonic()
            dispatcher.drain()
            poll_all(grace)
            for r in robots.values():
                tick_robot(r, bus)
            # Dormir lo que falte del periodo, en trozos cortos para reaccionar a señales.
            while not stop and (time.monotonic() - t0) < args.period:
                time.sleep(min(0.1, args.period))
    finally:
        pool.shutdown(wait=False)
        if web is not None:
            web.stop()
        bus.stop()
    return 0


def tick_robot(r: Robot, bus: MqttBus) -> None:
    """Parte del tick sin red: construir y publicar el `state` (tras `poll_all`)."""
    t = r.last_telemetry
    header = bus.next_header(r.serial, "state")
    state = to_vda_state(header, t, r.overlay(), error=r.last_error)
    bus.publish_raw(r.serial, "state", state.to_dict())
    if t is not None:
        acts = ",".join(f"{a.actionType}:{a.actionStatus}" for a in state.actionStates) or "-"
        x, y = t.pose[:2] if t.pose else (float("nan"), float("nan"))
        dbg = " ".join(i.infoDescriptor or "" for i in t.information if i.infoLevel == "DEBUG")
        r.log.info("state hdr=%d pos=(%.2f,%.2f) bat=%.1f%% mode=%s drv=%d chg=%d busy=%d/%d/%d order=%s/%d acts=%s errs=%d %s",
                   state.headerId, x, y, t.battery, t.operating_mode, t.driving,
                   state.powerSupply.charging, r.orders.busy, r.charge.active, t.foreign_busy,
                   state.orderId or "-", state.orderUpdateId, acts, len(state.errors), dbg)
    else:
        r.log.info("state hdr=%d SIN telemetría errs=%d", state.headerId, len(state.errors))


if __name__ == "__main__":
    sys.exit(main())
