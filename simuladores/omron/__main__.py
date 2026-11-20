"""Arranque del OMRON digital: `python -m simuladores.omron [opciones]`.

Ejemplos:

    # como el LD real: puerto 7171, en el cargador y localizado
    python -m simuladores.omron

    # igual que lo encontramos el 2026-11-20 (sin localizar: no coge trabajos)
    python -m simuladores.omron --perdido

    # diez veces más rápido, con poca batería para ver el autodock
    python -m simuladores.omron --acelerar 10 --soc 27 --fuera-del-dock descanso
"""
from __future__ import annotations

import argparse
import logging
import os
import signal

from .mapa import leer_mapa
from .robot import DATOS, OmronDigital, poner_config
from .servidor import ServidorARCL


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m simuladores.omron",
                                description="OMRON LD digital: servidor ARCL que responde como el robot real.")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=7171, help="puerto ARCL (7171 en el robot real)")
    p.add_argument("--password", default=os.environ.get("OMRON_DIGITAL_PASSWORD", "digital"),
                   help="contraseña ARCL (o OMRON_DIGITAL_PASSWORD; por defecto 'digital')")
    p.add_argument("--map", default=str(DATOS / "aula_ejemplo.map"), help="fichero .map de MobilePlanner")
    p.add_argument("--nombre", default="Omron-LD", help="nombre del robot en la cola")
    p.add_argument("--dock", default="-6415,566,-80", help="pose del cargador: x,y,θ (mm, mm, grados)")
    p.add_argument("--soc", type=float, default=99.6, help="batería inicial (%%)")
    p.add_argument("--perdido", action="store_true", help="arranca sin localizar (LocalizationScore 0)")
    p.add_argument("--fuera-del-dock", metavar="GOAL", help="arranca en ese goal en vez de en el cargador")
    p.add_argument("--velocidad", type=float, default=1000.0, help="mm/s en recta")
    p.add_argument("--acelerar", type=float, default=1.0, help="factor de tiempo (10 = diez veces más rápido)")
    p.add_argument("--autodock", choices=["true", "false"], help="AutoDock de la sección Docking")
    p.add_argument("--autodock-soc", type=float, help="AutoDockStateOfCharge (%%)")
    p.add_argument("--autodock-idle", type=float, help="AutoDockIdleTime (minutos)")
    p.add_argument("--dock-hasta-cargar", choices=["true", "false"], help="DockUntilDoneCharging")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s [omron-digital] %(message)s")
    mapa = leer_mapa(a.map)
    robot = OmronDigital(mapa=mapa, nombre=a.nombre, password=a.password,
                         dock=tuple(float(v) for v in a.dock.split(",")),
                         soc=a.soc, localizado=not a.perdido, velocidad=a.velocidad)
    docking = robot.config["Docking"]
    for clave, valor in (("AutoDock", a.autodock), ("AutoDockStateOfCharge", a.autodock_soc),
                         ("AutoDockIdleTime", a.autodock_idle),
                         ("DockUntilDoneCharging", a.dock_hasta_cargar)):
        if valor is not None:
            poner_config(docking, clave, f"{valor:g}" if isinstance(valor, float) else valor)
    if a.fuera_del_dock:
        if a.fuera_del_dock not in mapa.goals:
            p.error(f"no hay goal '{a.fuera_del_dock}' en {mapa.fichero}: {', '.join(mapa.goals)}")
        g = mapa.goals[a.fuera_del_dock]
        robot.x, robot.y, robot.th = g.x, g.y, g.th
        robot.docking = "Undocked"
        robot.modo = "stop"
        robot.carga_estado = robot._estado_carga()
        robot.estado_texto = robot.extendido = f"Arrived at {g.nombre}"

    servidor = ServidorARCL((a.host, a.port), robot, acelerar=a.acelerar)

    def _terminar(*_):
        raise KeyboardInterrupt          # SIGTERM (kill, docker stop) igual que Ctrl-C

    signal.signal(signal.SIGTERM, _terminar)
    logging.info("OMRON digital '%s' en %s:%d (mapa %s, goals %s, rutas %s)", robot.nombre, a.host,
                 servidor.puerto, mapa.fichero, list(mapa.goals), list(mapa.rutas))
    try:
        servidor.servir()
    except KeyboardInterrupt:
        logging.info("parado")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
