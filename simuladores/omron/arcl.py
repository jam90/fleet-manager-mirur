"""Intérprete de comandos ARCL del OMRON digital.

`Sesion.responder(linea)` recibe una línea tal cual llega por el socket y
devuelve las líneas de respuesta (sin `\\r\\n`). No hace red: el servidor
(servidor.py) es quien lee y escribe en el socket.

Reglas generales de ARCL, vistas en el LD real (2026-11-20):

- El nombre del comando no distingue mayúsculas (`STATUS`, `getgoals`).
- Cada comando tiene un alias corto (`qp` = `queuePickup`), el que va entre
  corchetes en `help`.
- Una línea vacía no tiene respuesta.
- Comando desconocido: `Unknown command <comando>`.
- Error de un comando conocido: dos líneas,
  `CommandError: <línea tal cual>` y `CommandErrorDescription: <motivo>`.
"""
from __future__ import annotations

import re
import shlex

from .robot import DATOS, OmronDigital

# --- ayuda: la lista de comandos del LD real (datos/ayuda.txt) ----------------

_AYUDA = (DATOS / "ayuda.txt").read_text()
BIENVENIDA = _AYUDA.splitlines()                      # lo que se envía tras la contraseña
_ENTRADAS: list[tuple[str, str | None, str]] = []      # (nombre, alias, línea tal cual)
for _l in BIENVENIDA:
    _m = re.match(r"^\s*(\S+)\s{2,}(.*)$", _l)
    if _m and _l not in ("Commands:",):
        _alias = re.search(r"\[(\w+)\]\s*$", _m.group(2))
        _ENTRADAS.append((_m.group(1), _alias.group(1) if _alias else None, _l))
COMANDOS = {n.lower(): n for n, _, _ in _ENTRADAS}
ALIAS = {a.lower(): n for n, a, _ in _ENTRADAS if a}

# Lo que lista getInfoList en el LD real, en su orden.
INFO_LIST = ["RobotIP", "WirelessLink", "WirelessQuality", "Local Plan Time", "Local DWA Time",
             "Num New Obstacle Points", "Closest Obs Distance", "PlanTime Limited Lin Vel",
             "Local Plan Area Size X", "Local Plan Area Size Y", "Odometer(KM)", "HourMeter",
             "Temperature(C)", "LaserScore", "LaserLock", "LaserNumSamples", "LaserNumPeaks",
             "ChargeEstimate", "CurrentDraw", "PackVoltage", "BatteryStatus", "BatteryError",
             "OnCharger", "ChargeState", "MPacs", "Laser_1 Pacs", "Laser_1 Filtered_v2",
             "Laser_2 Pacs", "Laser_2 Filtered_v2", "SBC Uptime", "ARAM Uptime", "Idle",
             "TipAngle", "IsTipped", "Queue ID", "Queue Job ID", "DebugLogState",
             "DebugLogSeconds"]

TIPOS_CONSULTA = "Valid querytypes are <id> <jobid> <robotname> <status> "    # visto en el LD (espacio final incluido)
TIPOS_CANCELAR = "Valid canceltypes are <id> <jobid> <robotname> <status> "   # visto en el LD (espacio final incluido)


def _lineas(fichero: str) -> list[str]:
    return (DATOS / fichero).read_text().splitlines()


class ErrorComando(Exception):
    """Se convierte en el par CommandError / CommandErrorDescription."""


class Sesion:
    """Una conexión ARCL ya autenticada."""

    def __init__(self, robot: OmronDigital):
        self.robot = robot
        self.eco = False
        self.cerrar = False

    # --- entrada ---

    def responder(self, linea: str) -> list[str]:
        linea = linea.strip("\r\n")
        if not linea.strip():
            return []                                     # visto en el LD: sin respuesta
        palabra, _, resto = linea.strip().partition(" ")
        clave = palabra.lower()
        nombre = COMANDOS.get(clave) or ALIAS.get(clave)
        if nombre is None:
            nombre = OCULTOS.get(clave) or _PROPIOS.get(clave)
        if nombre is None:
            return [f"Unknown command {palabra}"]         # visto en el LD
        try:
            args = shlex.split(resto)
        except ValueError:
            args = resto.split()
        metodo = getattr(self, "c_" + nombre, None)
        try:
            if metodo is None:
                raise ErrorComando(f"{nombre} no está implementado en el OMRON digital")
            salida = metodo(args, resto.strip())
        except ErrorComando as e:
            return [f"CommandError: {linea.strip()}", f"CommandErrorDescription: {e}"]
        return ([linea.strip()] if self.eco else []) + salida

    # --- ayuda y sesión ---

    def c_help(self, args, resto):
        if not args:
            return BIENVENIDA[2:]                         # "Commands:" … "End of commands"
        p = args[0].lower()
        sel = [l for n, a, l in _ENTRADAS if n.lower().startswith(p) or (a and a.lower().startswith(p))]
        return [f"Commands and aliases with prefix '{args[0]}':", *sel, "End of commands"]

    def c_quit(self, args, resto):
        self.cerrar = True
        return ["Closing connection"]                     # visto en el LD

    def c_echo(self, args, resto):
        if args and args[0].lower() in ("on", "off"):
            self.eco = args[0].lower() == "on"
            return [f"Echo turned {args[0].lower()}"]     # supuesto
        return [f"Echo is {'on' if self.eco else 'off'}."]    # visto en el LD

    def c_arclStat(self, args, resto):
        return ["Checking folder /proc/4162/fd", "FileHandleUsage: 39/500"]   # visto en el LD

    def c_getDateTime(self, args, resto):
        return [f"DateTime: {self.robot.reloj().strftime('%m/%d/%Y %H:%M:%S')}"]

    # --- estado ---

    def _temperatura(self) -> int:
        return 39                                         # visto en el LD, en el dock

    def c_status(self, args, resto):
        r = self.robot
        ext, st = r.texto_status()
        return [f"ExtendedStatusForHumans: {ext}",
                f"Status: {st}",
                f"StateOfCharge: {r.soc:.1f}",
                f"Location: {r.x:.0f} {r.y:.0f} {r.th:.0f}",
                f"LocalizationScore: {r.score:.6f}",
                f"Temperature: {self._temperatura()}"]

    def c_oneLineStatus(self, args, resto):
        r = self.robot
        _, st = r.texto_status()
        return [f"Status: {st} StateOfCharge: {r.soc:.1f} Location: {r.x:.0f} {r.y:.0f} "
                f"{r.th:.0f} Temperature: {self._temperatura()}"]

    def c_queryDockStatus(self, args, resto):
        r = self.robot
        return [f"DockingState: {r.docking} ForcedState: {r.forzado} ChargeState: {r.carga_estado}"]

    def c_queryMotors(self, args, resto):
        if self.robot.estop:
            return ["EStop pressed"]                      # supuesto
        return ["Motors enabled" if self.robot.motores else "Motors disabled"]

    def c_enableMotors(self, args, resto):
        if self.robot.estop:
            raise ErrorComando("Cannot enable motors while the estop is pressed")   # supuesto
        self.robot.habilitar_motores()
        return []                     # la respuesta son los eventos "Motors enabled", "Stopped"…

    def c_modeQuery(self, args, resto):
        r = self.robot
        if r.modo == "dock":
            return ['ModeQuery: dock "Dock" true true false']           # visto en el LD (Docked)
        if r.modo == "goal":
            # visto en el LD: en marcha y también tras fallar; se queda en este modo
            return ['ModeQuery: Goal "Goal seeking" false true false']
        if r.modo == "seta":
            return ['ModeQuery: MotorsAndEStop "EStop pressed" true false false']   # visto en el LD
        if r.modo == "motores":
            return ['ModeQuery: MotorsAndEStop "EStop relieved but motors still disabled" '
                    'true false false']                                  # visto en el LD
        return ['ModeQuery: stop "Stop" false true false']              # visto en el LD

    def c_faultsGet(self, args, resto):
        lineas = [f'FaultList: "{n}" "{c}" "{l}"' for n, (c, l) in self.robot.fallos_app.items()]
        return lineas + ["End of FaultList"]              # visto en el LD (vacía)

    def c_queryFaults(self, args, resto):
        lineas = [f'RobotFaultQuery: "{self.robot.nombre}" "{n}" "{c}"'
                  for n, (c, _) in self.robot.fallos_app.items()]         # supuesto
        return lineas + ["EndQueryFaults"]                # visto en el LD (vacía)

    def c_applicationFaultQuery(self, args, resto):
        lineas = [f'ApplicationFaultQuery: "{n}" "{c}" "{l}"'
                  for n, (c, l) in self.robot.fallos_app.items()]         # supuesto
        return lineas + ["End of ApplicationFaultQuery"]  # visto en el LD (vacía)

    def c_applicationFaultSet(self, args, resto):
        if len(args) < 3:
            raise ErrorComando('Arguments are <name> "<short description>" "<long description>" '
                               '<bool_driving> <bool_critical>')          # supuesto
        self.robot.fallos_app[args[0]] = (args[1], args[2])
        return [f"ApplicationFaultSet: {args[0]}"]        # supuesto

    def c_applicationFaultClear(self, args, resto):
        nombre = args[0] if args else ""
        if self.robot.fallos_app.pop(nombre, None) is None:
            raise ErrorComando(f"There is no fault {nombre} to clear")    # visto en el LD
        return [f"ApplicationFaultClear: {nombre}"]       # supuesto

    def c_applicationBlockDrivingSet(self, args, resto):
        if len(args) < 3:
            raise ErrorComando('Arguments are <name> "<short description>" "<long description>"')  # visto
        self.robot.bloquear(args[0], args[1], args[2])
        return [f"ApplicationBlockDrivingSet set {args[0]}"]  # visto en el LD

    def c_applicationBlockDrivingClear(self, args, resto):
        nombre = args[0] if args else ""
        if not self.robot.desbloquear(nombre):
            raise ErrorComando(f"There is no blockDriving {nombre} to clear")   # visto en el LD
        return [f"ApplicationBlockDrivingClear cleared {nombre}"]   # visto en el LD

    def c_centralServer(self, args, resto):
        return ["CentralServer: none"]                    # visto en el LD

    def c_wapInfo(self, args, resto):
        return []                                         # visto en el LD: nada

    def c_odometer(self, args, resto):
        r = self.robot
        return [f"Odometer: {r.odo_mm:.0f} mm {r.odo_deg:.0f} deg {r.uptime:.0f} sec"]

    def c_odometerReset(self, args, resto):
        self.robot.odo_mm = self.robot.odo_deg = 0.0
        return ["Reset odometer"]                         # supuesto

    def c_getPoseEncoder(self, args, resto):
        x, y, th = self.robot.encoder
        return [f"PoseEncoder: {x:.0f} {y:.0f} {th:.2f}"]

    def c_goalDistanceRemainingLocal(self, args, resto):
        r = self.robot
        p = r.paso
        if p and p.tipo == "goto":
            g = r.mapa.goals[p.goal]
            d = ((g.x - r.x) ** 2 + (g.y - r.y) ** 2) ** 0.5
            return [f'GoalDistanceRemainingLocal: "{p.goal}" {d:.0f}']
        return ['GoalDistanceRemainingLocal: "" -1 ']     # visto en el LD (espacio final incluido)

    def c_waitTaskState(self, args, resto):
        return ["WaitState: Not waiting"]                 # visto en el LD

    def c_payloadSlotCountLocal(self, args, resto):
        return [f'PayloadSlotCount: "{self.robot.nombre}" 0 ""', "EndPayloadSlotCount"]   # visto

    def c_payloadQueryLocal(self, args, resto):
        return ["EndPayloadQuery"]                        # visto en el LD

    def c_getInfoList(self, args, resto):
        return [f"InfoList: {n}" for n in INFO_LIST] + ["End of info list"]   # visto en el LD

    def c_getInfo(self, args, resto):
        nombre = resto.strip()
        valores = self._info()
        if nombre not in valores:
            raise ErrorComando(f"No info '{nombre}'")     # visto en el LD
        v = valores[nombre]
        return [f"Info: {nombre} {v}"]                   # visto en el LD: con espacio final si está vacío

    def _info(self) -> dict[str, str]:
        """Valores de getInfo con el formato del LD (unidades pegadas al valor)."""
        r = self.robot
        en_dock = r.docking == "Docked"
        t = r.trabajo_en_curso
        return {
            "RobotIP": r.ip, "WirelessLink": "true", "WirelessQuality": "58%",
            "Local Plan Time": "0", "Local DWA Time": "0", "Num New Obstacle Points": "0",
            "Closest Obs Distance": "0", "PlanTime Limited Lin Vel": "0",
            "Local Plan Area Size X": "0", "Local Plan Area Size Y": "0",
            "Odometer(KM)": f"{78 + r.odo_mm / 1e6:.0f}",
            "HourMeter": f"{3040 + r.uptime / 3600:.0f}",
            "Temperature(C)": str(self._temperatura()),
            "LaserScore": f"{r.score * 100:.0f}",          # supuesto: la escala
            "LaserLock": "0", "LaserNumSamples": "0", "LaserNumPeaks": "0",
            "ChargeEstimate": f"{r.soc:.2f} %",
            "CurrentDraw": f"{1.49 if en_dock else (6.2 if r.moviendose else 2.1):.2f} a",
            "PackVoltage": f"{24.0 + 3.67 * r.soc / 100:.3f} v",
            "BatteryStatus": "0x50a9", "BatteryError": "0x0000",
            "OnCharger": "true" if en_dock else "false",
            "ChargeState": str({"Not": 0, "Bulk": 1, "Overcharge": 2, "Float": 3}[r.carga_estado]),
            "MPacs": "0", "Laser_1 Pacs": "0", "Laser_1 Filtered_v2": "0",
            "Laser_2 Pacs": "0", "Laser_2 Filtered_v2": "0",
            "SBC Uptime": f"{66000 + r.uptime:.0f}", "ARAM Uptime": f"{65950 + r.uptime:.0f}",
            "Idle": f"{r.idle:.0f}", "TipAngle": "1.030 deg", "IsTipped": "false",
            "Queue ID": t.id if t else "", "Queue Job ID": t.job_id if t else "",
            "DebugLogState": "0", "DebugLogSeconds": "0",
        }

    # --- configuración ---

    def c_getConfigSectionList(self, args, resto):
        return _lineas("secciones.txt")

    def c_getConfigSectionValues(self, args, resto):
        nombre = resto.strip()
        secciones = [l.split(": ", 1)[1] for l in _lineas("secciones.txt") if ": " in l]
        if nombre not in secciones and nombre not in self.robot.config:
            raise ErrorComando(f"No section of name {nombre}")   # visto en el LD
        # De las secciones que no se capturaron del LD real solo sale el final.
        return list(self.robot.config.get(nombre, [])) + ["EndOfGetConfigSectionValues"]

    # --- mapa ---

    def c_getGoals(self, args, resto):
        return [f"Goal: {g}" for g in self.robot.mapa.goals] + ["End of goals"]   # visto

    def c_getRoutes(self, args, resto):
        return ["Routes", *(f"Route: {r}" for r in self.robot.mapa.rutas), "End of routes"]   # visto

    def c_getMacros(self, args, resto):
        return [f"Macro: {m}" for m in self.robot.mapa.macros] + ["End of macros"]   # visto

    def c_getMacroTemplates(self, args, resto):
        # visto en el LD: lista las macros, NO los templates (MacroTemplateFM no salía)
        return [f"MacroTemplate: {m}" for m in self.robot.mapa.macros] + ["End of MacroTemplate list"]

    def c_mapObjectTypeList(self, args, resto):
        return _lineas("tipos_objeto.txt")

    def c_mapObjectTypeInfo(self, args, resto):
        tipos = {l.split()[1]: l.split()[2] for l in _lineas("tipos_objeto.txt") if l.startswith("MapObjectTypeList:")}
        t = args[0] if args else ""
        lineas = []
        if t == "Goal":
            lineas = ['MapObjectTypeList: Goal GoalType "Objetivo" ""']          # visto en el LD
        elif t in tipos:
            lineas = [f'MapObjectTypeList: {t} {tipos[t]} "{t}" ""']              # supuesto
        return lineas + ["End of MapObjectTypeInfo"]

    def c_mapObjectList(self, args, resto):
        if not args:
            raise ErrorComando("Argument is <type> to list")      # visto en el LD
        objs = [o for o in self.robot.mapa.objetos if o.tipo == args[0]]
        return [f'MapObjectList: "{o.nombre}" {o.tipo}' for o in objs] + ["End of MapObjectList"]

    def c_mapObjectUpdate(self, args, resto):
        """visto en el LD: crea (o cambia) un objeto del mapa, que queda guardado en el
        fichero. Exige el robot parado y libre."""
        r = self.robot
        if r.pasos or r.parado_por_algo:
            raise ErrorComando("robot not idle, try again later")              # visto en el LD
        uso = ('argument error, expected: <Type>(WithHeading) <poseX> <poseY> <th> "<desc>" ICON '
               '"<name>" <fromX> <fromY> <toX> <toY> (<param1> <param2> ... <paramN>)')       # visto
        if len(args) < 7 or args[5] != "ICON":
            raise ErrorComando(uso)
        try:
            x, y, th = float(args[1]), float(args[2]), float(args[3])
        except ValueError:
            raise ErrorComando(uso)
        from .mapa import ObjetoMapa
        nuevo = ObjetoMapa(args[0], args[6], int(x), int(y), th, args[4])
        r.mapa.objetos = [o for o in r.mapa.objetos if o.nombre != nuevo.nombre] + [nuevo]
        r.eventos.append("Map changed")                                         # visto en el LD
        return ["Processing mapObjectUpdate, local map", "Completed mapObjectUpdate"]   # visto

    def c_mapObjectInfo(self, args, resto):
        if not args:
            raise ErrorComando("Argument is <name> to query (use quotes if there are spaces)")  # visto
        o = self.robot.mapa.objeto(args[0])
        if o is None:
            return ["End of MapObjectInfo"]               # visto en el LD
        lineas = [f'MapObjectInfo: "{o.nombre}" {o.tipo} "{o.descripcion}"',
                  f'MapObjectInfoCoord: "{o.nombre}" {o.x} {o.y} {o.th:.3f}']    # visto (goal)
        if o.esquinas:
            x1, y1, x2, y2 = o.esquinas
            lineas.append(f'MapObjectInfoCoord: "{o.nombre}" {x1} {y1} {x2} {y2}')   # supuesto
        return lineas + ["End of MapObjectInfo"]

    # --- movimiento directo ---

    def _goal(self, args, sin_goal: str) -> str:
        if not args:
            raise ErrorComando(sin_goal)
        if args[0] not in self.robot.mapa.goals:
            raise ErrorComando(f"No goal '{args[0]}'")    # visto en el LD (goto)
        return args[0]

    def c_goto(self, args, resto):
        self.robot.goto(self._goal(args, "There was no goal given"))   # visto en el LD
        return []                     # la respuesta es el evento "Going to …" (a todos)

    def c_dock(self, args, resto):
        if self.robot.docking == "Docked":
            return ["Already docked"]                     # supuesto
        self.robot.ir_al_dock(forzado=True)
        return []

    def c_undock(self, args, resto):
        self.robot.undock()
        return []

    def c_patrol(self, args, resto, una_vez=False):
        ruta = args[0] if args else ""
        if ruta not in self.robot.mapa.rutas:
            raise ErrorComando(f"Bad route name given ('{ruta}')")    # visto en el LD
        desde = int(args[1]) if una_vez and len(args) > 1 and args[1].isdigit() else 0
        self.robot.patrullar(ruta, una_vez=una_vez, desde=desde)
        return [f"Patrolling route {ruta}"]               # supuesto

    def c_patrolOnce(self, args, resto):
        return self.c_patrol(args, resto, una_vez=True)

    def c_stop(self, args, resto):
        self.robot.parar()
        return []

    def c_stay(self, args, resto):
        self.robot.parar()
        return []

    def c_go(self, args, resto):
        self.robot.seguir()
        return []

    def c_localizeAtGoal(self, args, resto):
        g = args[0] if args else ""
        if g not in self.robot.mapa.goals:
            raise ErrorComando(f"No goal matches '{g}'")  # visto en el LD
        self.robot.localizar_en(g)
        return [f"Localized to {g}"]                      # supuesto

    def c_executeMacro(self, args, resto):
        if not args:
            raise ErrorComando("There was no macro given")    # visto (executeMacroTemplate)
        if resto.strip() not in self.robot.mapa.macros:
            raise ErrorComando(f"No macro '{resto.strip()}'")     # visto en el LD
        self.robot.ejecutar_macro(resto.strip(), self.robot.mapa.macros[resto.strip()], template=False)
        return []                     # la respuesta es el evento "Executing macro …"

    def c_executeMacroTemplate(self, args, resto):
        if not args:
            raise ErrorComando("There was no macro given")    # visto en el LD
        pasos = self.robot.mapa.templates.get(args[0])
        if pasos is None:
            raise ErrorComando(f"No macro '{resto.strip()}'")     # visto en el LD (toda la línea)
        valores = args[1:]                                    # por posición; comillas agrupan (visto)

        def sustituir(v: str) -> str:
            return re.sub(r"\$(\d+)", lambda m: valores[int(m.group(1)) - 1]
                          if int(m.group(1)) <= len(valores) else "", v)
        self.robot.ejecutar_macro(args[0], [tuple(sustituir(v) for v in paso) for paso in pasos], template=True)
        return []

    def c_doTask(self, args, resto):
        if not args:
            raise ErrorComando("Must give task to do")    # visto en el LD
        raise ErrorComando(f"There was no task with name {args[0]}")   # visto (las tareas no se simulan)

    def c_doTaskInstant(self, args, resto):
        return self.c_doTask(args, resto)

    # --- cola ---

    def c_queuePickup(self, args, resto):
        if not args:
            raise ErrorComando("Invalid argument, should be queuePickup <goal_name> [priority] [job_id]")  # visto
        if args[0] not in self.robot.mapa.goals:
            raise ErrorComando(f'queuepickup no such goal "{args[0]}"')     # visto en el LD
        prioridad = 10
        if len(args) > 1:
            try:
                prioridad = int(args[1])
            except ValueError:
                raise ErrorComando(f"queuepickup bad priority \"{args[1]}\"")   # supuesto
        t = self.robot.encolar(args[0], prioridad, args[2] if len(args) > 2 else None)
        return [f'queuepickup goal "{t.goal}" with priority {t.prioridad} id {t.id} '
                f'and job_id {t.job_id} successfully queued']          # visto en el LD (sin coma)

    def c_queueMulti(self, args, resto):
        uso = ("should be queueMulti <number_of_goals> <number_of_fields_per_goal> <goal1> "
               "<goal1_args> <goal2> <goal2_args> ... <goalN> <goalN_args> [job_id]")
        if not args:
            raise ErrorComando(f"Invalid argument, {uso}")                       # visto en el LD
        if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
            raise ErrorComando(f"Invalid argument not enough arguments supplied, {uso}")   # visto (queueMulti 1)
        n, f = int(args[0]), int(args[1])
        campos = args[2:]
        if f < 2 or len(campos) < n * f:
            raise ErrorComando(f"Invalid argument not enough arguments supplied, {uso}")   # visto
        job_id = campos[n * f] if len(campos) > n * f else None
        segmentos = []
        for i in range(n):
            goal, tipo, *resto_campos = campos[i * f:(i + 1) * f]
            if goal not in self.robot.mapa.goals:
                raise ErrorComando(f'queuemulti no such goal "{goal}"')          # supuesto
            if tipo.lower() not in ("pickup", "dropoff"):
                raise ErrorComando(f'queuemulti bad goal type "{tipo}"')         # supuesto
            prio = int(resto_campos[0]) if resto_campos and resto_campos[0].lstrip("-").isdigit() else 10
            segmentos.append((goal, tipo.upper(), prio))
        if job_id is None:
            job_id = self.robot.nuevo_job_id()
        salida = []
        for goal, tipo, prio in segmentos:
            t = self.robot.encolar(goal, prio, job_id, tipo=tipo)
            salida.append(f'queuemulti goal "{goal}" with priority {prio} id {t.id} '
                          f'and job_id {job_id} successfully queued')   # supuesto
        return salida + ["EndQueueMulti"]                 # supuesto

    def _consulta(self, args, nombre: str):
        if len(args) < 2:
            raise ErrorComando(f"Invalid argument, should be {nombre} <querytype> <queryvalue> [echo_string]")  # visto
        try:
            trabajos = self.robot.buscar(args[0], args[1], local=nombre.endswith("Local"))
        except ValueError:
            raise ErrorComando(TIPOS_CONSULTA)
        return [f"QueueQuery: {t.campos(consulta=True)}" for t in trabajos] + ["EndQueueQuery"]   # visto

    def c_queueQueryLocal(self, args, resto):
        return self._consulta(args, "queueQueryLocal")

    def c_queueQuery(self, args, resto):
        return self._consulta(args, "queueQuery")

    def _cancelar(self, args, nombre: str):
        if len(args) < 2:
            raise ErrorComando(f'Invalid argument, should be {nombre} <canceltype> <cancelvalue> '
                               f'[echo_string_or_"default"] [reason]')   # visto
        tipo, valor = args[0], args[1]
        eco = args[2] if len(args) > 2 and args[2] != "default" else ""
        motivo = args[3] if len(args) > 3 else "None"
        try:
            trabajos = self.robot.buscar(tipo, valor)
        except ValueError:
            raise ErrorComando(TIPOS_CANCELAR)
        d = f'"{tipo}" "{valor}" "{eco}" "{motivo}"'
        salida = [f"queuecancel attempting to cancel {d}",            # visto en el LD
                  f"queuecancel cancelling {d} from queue"]            # visto en el LD
        if not self.robot.cancelar(trabajos, motivo):
            salida.append(f'queuecancel has failed to cancel {d} failure cause "no matching job"')   # visto
        return salida

    def c_queueModifyLocal(self, args, resto):
        if len(args) < 3:
            # visto en el LD, con su errata ("prioirty")
            raise ErrorComando("Invalid argument, should be queueModifyLocal <id> <modify_type> "
                               "<goal_name_or_prioirty>")
        raise ErrorComando("queueModifyLocal no está implementado en el OMRON digital")

    def c_queueCancelLocal(self, args, resto):
        return self._cancelar(args, "queueCancelLocal")

    def c_queueCancel(self, args, resto):
        return self._cancelar(args, "queueCancel")

    def _robot_cola(self) -> str:
        estado, sub = self.robot.estado_cola()
        return f'QueueRobot: "{self.robot.nombre}" {estado} {sub} ""'   # visto en el LD

    def c_queueShow(self, args, resto):
        vivos = [f"QueueShow: {t.campos()}" for t in self.robot.cola if t.vivo]   # supuesto
        return vivos + [self._robot_cola(), "EndQueueShow"]   # visto en el LD (sin trabajos)

    def c_queueShowCompleted(self, args, resto):
        hechos = [f"QueueShowCompleted: {t.campos(consulta=True)}" for t in self.robot.cola if not t.vivo]   # visto
        return hechos + ["EndQueueShowCompleted"]          # visto en el LD (vacía)

    def c_queueShowRobot(self, args, resto):
        return [self._robot_cola(), "EndQueueShowRobot"]   # visto en el LD

    def c_queueShowRobotLocal(self, args, resto):
        return self.c_queueShowRobot(args, resto)

    # --- E/S ---

    def _io(self, fichero: str, prefijo: str) -> list[str]:
        return [l.split(": ", 1)[1] for l in _lineas(fichero) if l.startswith(prefijo)]

    def c_inputList(self, args, resto):
        return _lineas("entradas.txt")

    def c_outputList(self, args, resto):
        return _lineas("salidas.txt")

    def c_analogInputList(self, args, resto):
        return _lineas("entradas_analogicas.txt")

    def c_inputQuery(self, args, resto):
        if not args or args[0] not in self._io("entradas.txt", "InputList:"):
            raise ErrorComando(f"No input '{args[0] if args else ''}'")   # supuesto
        return [f"Input: {args[0]} off"]                  # supuesto

    def _salida(self, args, valor: bool | None):
        if not args or args[0] not in self._io("salidas.txt", "OutputList:"):
            raise ErrorComando(f"No output '{args[0] if args else ''}'")  # supuesto
        if valor is not None:
            self.robot.salidas[args[0]] = valor
        return [f"Output: {args[0]} {'on' if self.robot.salidas.get(args[0]) else 'off'}"]   # supuesto

    def c_outputQuery(self, args, resto):
        return self._salida(args, None)

    def c_outputOn(self, args, resto):
        return self._salida(args, True)

    def c_outputOff(self, args, resto):
        return self._salida(args, False)

    # --- solo del digital: no existen en el robot real (ver README) ---

    def c_digitalEstop(self, args, resto):
        pulsada = bool(args) and args[0].lower() == "on"
        self.robot.pulsar_seta(pulsada)
        return [f"digital: estop {'on' if pulsada else 'off'}"]

    def c_digitalSoc(self, args, resto):
        try:
            self.robot.soc = max(0.0, min(100.0, float(args[0])))
        except (IndexError, ValueError):
            raise ErrorComando("digitalSoc <porcentaje>")
        return [f"digital: StateOfCharge {self.robot.soc:.1f}"]

    def c_digitalLost(self, args, resto):
        self.robot.perder_localizacion()
        return ["digital: LocalizationScore 0"]


# Comandos del LD real que funcionan pero no salen en `help` (visto: goto).
OCULTOS = {"goto": "goto"}

# Comandos solo del digital: tampoco salen en `help`.
_PROPIOS = {"digitalestop": "digitalEstop", "digitalsoc": "digitalSoc", "digitallost": "digitalLost"}
