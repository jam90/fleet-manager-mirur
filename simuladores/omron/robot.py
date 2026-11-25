"""Modelo del OMRON LD digital: estado, movimiento, batería, autodock y cola.

Es puro: no abre sockets ni lee el reloj por su cuenta. El servidor llama a
`avanzar(dt)` unas veces por segundo y reparte a todos los clientes ARCL lo
que se haya acumulado en `eventos` (los mensajes asíncronos del robot real:
"Going to …", "Arrived at …", "QueueUpdate: …").

Unidades como en ARCL: mm, grados y segundos. El FM es quien pasa a m y rad.

Qué está copiado del LD real y qué es supuesto: ver README.md. En el
código, `# visto en el LD` marca lo comprobado contra el robot y
`# supuesto` lo que sale de la documentación o de cómo creemos que se
comporta, para corregirlo el día que se pueda probar.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .mapa import Mapa

DATOS = Path(__file__).parent / "datos"

# ChargeState en texto (status) y en número (getInfo ChargeState).
# visto en el LD: "Float" ↔ 3. supuesto: el resto del orden.
ESTADOS_CARGA = {"Not": 0, "Bulk": 1, "Overcharge": 2, "Float": 3}

# Puntuación de localización por debajo de la cual el robot está "Lost".
# visto en el LD: con 0 la cola dice "UnAvailable Lost". supuesto: el umbral.
UMBRAL_LOCALIZADO = 0.2


def fecha_arcl(t: datetime | None) -> str:
    """Fecha y hora como las escribe ARCL (visto en getDateTime: 11/20/2026 09:03:04)."""
    return t.strftime("%m/%d/%Y %H:%M:%S") if t else "None None"


# --- configuración (secciones de getConfigSectionValues) ----------------------

def cargar_config(password: str, mapa: str) -> dict[str, list[str]]:
    """Secciones de configuración, como líneas `GetConfigSectionValue: …` tal cual
    las da el LD real (datos/config/*.txt). `Files` se genera con el mapa cargado
    y `ARCL server setup` con la contraseña del digital."""
    secciones: dict[str, list[str]] = {}
    for f in sorted((DATOS / "config").glob("*.txt")):
        texto = f.read_text().replace("{password}", password)
        secciones[f.stem] = [l for l in texto.splitlines() if l and not l.startswith("EndOf")]
    secciones["Files"] = [f"GetConfigSectionValue: Map {mapa}"]
    return secciones


def valor_config(lineas: list[str], clave: str) -> str | None:
    for l in lineas:
        partes = l.split(None, 2)          # "GetConfigSectionValue:", clave, valor
        if len(partes) >= 2 and partes[1] == clave:
            return partes[2] if len(partes) == 3 else ""
    return None


def poner_config(lineas: list[str], clave: str, valor: str) -> None:
    for i, l in enumerate(lineas):
        partes = l.split(None, 2)
        if len(partes) >= 2 and partes[1] == clave:
            lineas[i] = f"GetConfigSectionValue: {clave} {valor}"
            return
    lineas.append(f"GetConfigSectionValue: {clave} {valor}")


# --- cola de trabajos ---------------------------------------------------------

@dataclass
class Trabajo:
    """Un segmento de la cola (en ARCL, cada goal de un trabajo es un segmento
    con su `id`; los segmentos de un mismo trabajo comparten `job_id`)."""
    id: str                     # PICKUP1, DROPOFF2…
    job_id: str                 # lo elige quien encola (el FM); si no, JOB<n>
    prioridad: int
    goal: str
    encolado: datetime
    orden: int                  # desempate por orden de llegada
    estado: str = "Pending"     # Pending | InProgress | Interrupted | Completed | Cancelled | Failed
    subestado: str = "None"
    robot: str = "None"
    terminado: datetime | None = None
    fallos: int = 0

    @property
    def vivo(self) -> bool:
        return self.estado in ("Pending", "InProgress", "Interrupted")

    def campos(self, consulta: bool = False) -> str:
        # visto en el LD: QueueQuery/QueueShowCompleted llevan un "" antes de los fallos
        # que QueueUpdate no lleva; QueueUpdate acaba en espacio (lo pone _actualizar).
        return (f'{self.id} {self.job_id} {self.prioridad} {self.estado} {self.subestado} '
                f'Goal "{self.goal}" "{self.robot}" {fecha_arcl(self.encolado)} '
                f'{fecha_arcl(self.terminado)} {chr(34) * 2 + " " if consulta else ""}{self.fallos}')


# --- movimiento -----------------------------------------------------------------

@dataclass
class Paso:
    """Una cosa que el robot está haciendo o hará: salir del dock, ir a un goal,
    o ir al dock."""
    tipo: str                           # "undock" | "goto" | "punto" | "dock" | "wait" | "fin_macro"
    goal: str | None = None
    punto: tuple[float, float, float | None] | None = None   # "punto" (gotoPoint): x, y en mm; θ en grados
    macro: str | None = None            # paso de una macro: su nombre (los goto no dan mensajes)
    segundos: float = 0.0               # "wait"
    cerca: bool = False                 # "dock": ya junto al cargador, buscándolo
    trabajo: Trabajo | None = None
    forzado: bool = False               # dock pedido con el comando `dock`
    t: float = 0.0                      # segundos en este paso (para las maniobras)
    empezado: bool = False


@dataclass
class OmronDigital:
    mapa: Mapa
    nombre: str = "Omron-LD"                         # visto en el LD: QueueRobot "Omron-LD"
    password: str = "digital"
    ip: str = "127.0.0.1"
    dock: tuple[float, float, float] = (-6415, 566, -80)   # visto en el LD: pose al estar en el cargador
    soc: float = 99.6
    localizado: bool = True
    velocidad: float = 1000.0                        # mm/s en recta (el LD llega a 1350)
    giro: float = 60.0                               # °/s
    # batería, en %/s. supuesto: del orden de un LD real (≈13 h de autonomía, ≈3,5 h de carga)
    gasto_parado: float = 0.0012
    gasto_marcha: float = 0.0025
    carga: float = 0.008
    reloj: Callable[[], datetime] = datetime.now

    def __post_init__(self) -> None:
        # visto en el LD: con un dock dibujado en el mapa (DockLynx "DOCK"), los
        # textos llevan su nombre; sin él, van sin nombre.
        dock_mapa = next((o for o in self.mapa.objetos if o.tipo.startswith("Dock")), None)
        self.nombre_dock = dock_mapa.nombre if dock_mapa else ""
        if dock_mapa is not None and self.dock == (-6415, 566, -80):
            self.dock = (dock_mapa.x, dock_mapa.y, dock_mapa.th)
        self.x, self.y, self.th = self.dock
        self.score = 0.92 if self.localizado else 0.0
        self.docking = "Docked"                      # Undocked | Docking | Docked | Undocking
        self.forzado = "Unforced"
        self.motores = True
        self.estop = False
        self.estado_texto = "Stopped"                # lo que sale tras "Status:" fuera del dock
        self.modo = "dock"                           # para modeQuery: dock | goal | stop
        self.extendido = ""                          # ExtendedStatusForHumans fuera del dock
        self.bloqueos: dict[str, tuple[str, str]] = {}
        self.fallos_app: dict[str, tuple[str, str]] = {}
        self.pasos: list[Paso] = []
        self.patrulla: tuple[str, int, bool] | None = None   # (ruta, siguiente índice, repetir)
        self.cola: list[Trabajo] = []
        self.n_segmentos = 0
        self.n_trabajos = 0
        self.retenido = 0.0                          # s que espera antes de coger trabajos (stop/stay)
        self.idle = 0.0                              # visto en el LD: getInfo Idle, en s, crece en el dock
        self.uptime = 0.0
        self.odo_mm = 0.0
        self.odo_deg = 0.0
        self.encoder = [0.0, 0.0, 0.0]
        self.eventos: list[str] = []
        self.salidas: dict[str, bool] = {}
        self.config = cargar_config(self.password, self.mapa.fichero)
        self.carga_estado = self._estado_carga()

    # --- lectura de la configuración de carga (sección Docking) ---

    def _cfg(self, clave: str, defecto: str) -> str:
        v = valor_config(self.config.get("Docking", []), clave)
        return defecto if v is None else v

    @property
    def autodock(self) -> bool:
        return self._cfg("AutoDock", "true") == "true"

    @property
    def autodock_soc(self) -> float:
        return float(self._cfg("AutoDockStateOfCharge", "25"))

    @property
    def autodock_idle_min(self) -> float:
        return float(self._cfg("AutoDockIdleTime", "15"))

    @property
    def dock_hasta_cargar(self) -> bool:
        return self._cfg("DockUntilDoneCharging", "true") == "true"

    # --- estado que consultan los comandos ---

    @property
    def perdido(self) -> bool:
        return self.score < UMBRAL_LOCALIZADO

    @property
    def paso(self) -> Paso | None:
        return self.pasos[0] if self.pasos else None

    @property
    def trabajo_en_curso(self) -> Trabajo | None:
        for t in self.cola:
            if t.estado == "InProgress":
                return t
        return None

    @property
    def moviendose(self) -> bool:
        p = self.paso
        return bool(p and p.tipo in ("goto", "punto", "dock") and self.docking != "Docked"
                    and not self.parado_por_algo)

    @property
    def parado_por_algo(self) -> bool:
        return self.estop or not self.motores or bool(self.bloqueos)

    def _estado_carga(self) -> str:
        if self.docking != "Docked":
            return "Not"                     # supuesto
        if self.soc < 90:
            return "Bulk"
        if self.soc < 99:
            return "Overcharge"
        return "Float"

    def cargando_sin_terminar(self) -> bool:
        """visto en el LD: aun con DockUntilDoneCharging, un trabajo de la cola lo
        saca del dock cargando (PRUEBA-1, en Overcharge). No bloquea nunca."""
        return False

    def estado_cola(self) -> tuple[str, str]:
        """Estado y subestado del robot en la cola (QueueRobot)."""
        if self.estop:
            return "UnAvailable", "EStopPressed"         # supuesto
        if not self.motores:
            return "UnAvailable", "Interrupted"          # visto en el LD (seta soltada)
        if self.perdido:
            return "UnAvailable", "Lost"                 # visto en el LD
        if self.trabajo_en_curso:
            return "InProgress", "Driving"               # supuesto
        if self.bloqueos:
            return "UnAvailable", f"Fault_Driving_Application_{next(iter(self.bloqueos))}"   # visto
        if self.docking == "Docking":
            return "UnAvailable", "DockingRoutine"       # visto en el LD
        if self.pasos or self.patrulla:
            # visto en el LD: movido a mano (sin trabajo de la cola) → "UnAvailable Interrupted"
            return "UnAvailable", "Interrupted"
        if self.cargando_sin_terminar():
            return "UnAvailable", "Charging"             # supuesto
        if self.retenido > 0:
            return "UnAvailable", "Interrupted"          # supuesto
        return "Available", "Available"                  # supuesto

    def texto_status(self) -> tuple[str, str]:
        """(ExtendedStatusForHumans, Status) de `status`."""
        if self.estop:
            return "EStop pressed", "EStop pressed"      # supuesto (el evento sí está visto)
        if not self.motores:
            t = "EStop relieved but motors still disabled"                       # visto en el LD
            return t, t
        if self.docking in ("Docking", "Undocking"):
            # visto en el LD: Status es la línea del dock; el texto, según la fase
            if self.docking == "Undocking":
                humano = f"Decoupling from {self.nombre_dock or 'Charging Station'}"
            elif self.paso and self.paso.tipo == "dock" and not self.paso.cerca:
                humano = f"Driving to Charging Station {self.nombre_dock}".rstrip()
            else:
                humano = "Finding Charging Station Target"
            return (humano,
                    f"DockingState: {self.docking} ForcedState: {self.forzado} "
                    f"ChargeState: {self.carga_estado}")
        if self.docking == "Docked":
            # visto en el LD (con el espacio final incluido)
            # visto en el LD: con dock en el mapa "Charging, Connected to Charging Station DOCK";
            # con el mapa sin dock, "Charging, connected to Charging Station " (espacio final)
            ext = ((f"Charging, Connected to Charging Station {self.nombre_dock}" if self.nombre_dock
                    else "Charging, connected to Charging Station ")
                   if self.carga_estado != "Not" else "Docked ")
            return ext, (f"DockingState: Docked ForcedState: {self.forzado} "
                         f"ChargeState: {self.carga_estado}")
        if self.bloqueos:
            corto = next(iter(self.bloqueos.values()))[0]
            return corto, self.estado_texto              # visto en el LD: el texto corto
        return self.extendido or self.estado_texto, self.estado_texto

    # --- órdenes (las llaman los comandos ARCL) ---

    def _evento(self, texto: str) -> None:
        self.eventos.append(texto)

    def _evento_dock(self) -> None:
        # visto en el LD: al cambiar el estado del dock repite la línea de queryDockStatus
        self._evento(f"DockingState: {self.docking} ForcedState: {self.forzado} "
                     f"ChargeState: {self.carga_estado}")

    def _actualizar(self, t: Trabajo, estado: str, subestado: str = "None") -> None:
        t.estado, t.subestado = estado, subestado
        if estado in ("InProgress",):
            t.robot = self.nombre
        if not t.vivo:
            t.terminado = self.reloj()
        self._evento(f"QueueUpdate: {t.campos()} ")                # visto: espacio final

    def _cortar_movimiento(self) -> None:
        """visto en el LD: al cambiar de destino, parar o pulsar la seta a medio camino."""
        p = self.paso
        if p and p.tipo == "goto" and p.empezado:
            self._evento(f"Interrupted: Going to {p.goal}")

    def _interrumpir_trabajo(self) -> None:
        """Una orden directa (goto, dock, stop…) deja a medias el trabajo de la cola."""
        t = self.trabajo_en_curso
        if t:
            self._actualizar(t, "Interrupted")

    def _plan(self, *pasos: Paso) -> None:
        """Sustituye lo que estuviera haciendo. Si está en el dock y va a moverse,
        primero sale (visto en el LD: help undock, "done automatically too")."""
        self.pasos = []
        if self.docking == "Docking":       # iba al dock y aún no había llegado
            self.docking = "Undocked"
        if self.docking == "Docked" and pasos and pasos[0].tipo != "dock":
            self.pasos.append(Paso("undock"))
        self.pasos.extend(pasos)

    def goto(self, goal: str) -> None:
        self._cortar_movimiento()
        self._interrumpir_trabajo()
        self.patrulla = None
        self._plan(Paso("goto", goal=goal))

    def ir_al_dock(self, forzado: bool = True) -> None:
        self._interrumpir_trabajo()
        self.patrulla = None
        if self.docking == "Docked":
            return
        self.pasos = [Paso("dock", forzado=forzado)]

    def undock(self) -> None:
        self.patrulla = None
        if self.docking == "Docked":
            self.pasos = [Paso("undock")]

    def patrullar(self, ruta: str, una_vez: bool, desde: int = 0) -> None:
        self._interrumpir_trabajo()
        self.patrulla = (ruta, desde, not una_vez)
        self.pasos = []
        self._siguiente_de_patrulla()

    def _siguiente_de_patrulla(self) -> None:
        ruta, i, repetir = self.patrulla
        goals = self.mapa.rutas.get(ruta, [])
        if i >= len(goals):
            if repetir and goals:
                i = 0
            else:
                self.patrulla = None
                self._evento(f"Finished patrolling route {ruta}")       # supuesto
                return
        self.patrulla = (ruta, i + 1, repetir)
        self._plan(Paso("goto", goal=goals[i]))

    def parar(self, espera_s: float = 60.0) -> None:
        """stop / stay: para y no coge trabajos durante un rato (supuesto: 60 s)."""
        self._cortar_movimiento()
        self._interrumpir_trabajo()
        self.patrulla = None
        en_dock = self.docking == "Docked"
        self.pasos = []
        self.retenido = espera_s
        if not en_dock:
            self._quedar_parado()

    def _quedar_parado(self) -> None:
        # visto en el LD: "Stopping" y enseguida "Stopped"; modeQuery pasa a stop
        self.docking = "Undocked" if self.docking != "Docked" else self.docking
        self.modo = "stop"
        self.estado_texto = self.extendido = "Stopped"
        self._evento("Stopping")
        self._evento("Stopped")

    def seguir(self) -> None:
        """go: deja de esperar."""
        self.retenido = 0.0

    def localizar_en(self, goal: str) -> None:
        g = self.mapa.goals[goal]
        self.x, self.y, self.th = g.x, g.y, g.th
        self.score = 0.92
        if self.docking == "Docked":
            self.docking = "Undocked"
            self.carga_estado = self._estado_carga()
            self.modo = "stop"

    def encolar(self, goal: str, prioridad: int = 10, job_id: str | None = None,
                tipo: str = "PICKUP") -> Trabajo:
        self.n_segmentos += 1
        if job_id is None:
            self.n_trabajos += 1
            job_id = f"JOB{self.n_trabajos}"
        t = Trabajo(id=f"{tipo}{self.n_segmentos}", job_id=job_id, prioridad=prioridad,
                    goal=goal, encolado=self.reloj(), orden=self.n_segmentos)
        self.cola.append(t)
        self._evento(f"QueueUpdate: {t.campos()} ")
        return t

    def nuevo_job_id(self) -> str:
        self.n_trabajos += 1
        return f"JOB{self.n_trabajos}"

    def buscar(self, tipo: str, valor: str, local: bool = False) -> list[Trabajo]:
        """visto en el LD: las variantes Local solo ven los trabajos con este robot
        asignado; uno devuelto a la cola sin robot (tras un bloqueo) no sale."""
        if local:
            return [t for t in self.buscar(tipo, valor) if t.robot != "None" or t.estado != "Pending"
                    or t.subestado == "None"]
        tipo = tipo.lower()
        if tipo == "id":
            return [t for t in self.cola if t.id == valor]
        if tipo == "jobid":
            return [t for t in self.cola if t.job_id == valor]
        if tipo == "robotname":
            return [t for t in self.cola if t.robot == valor]
        if tipo == "status":
            return [t for t in self.cola if t.estado == valor]
        raise ValueError(tipo)

    def cancelar(self, trabajos: list[Trabajo], motivo: str = "None") -> list[Trabajo]:
        # visto en el LD: Cancelling → (Stopping) → Interrupted → Cancelled → Stopped
        hechos = []
        for t in trabajos:
            if not t.vivo:
                continue
            if t.estado == "InProgress":
                self._actualizar(t, "Cancelling", "None")
                self.pasos = []
                self._evento("Stopping")
                self._actualizar(t, "Interrupted", "None")
                self._actualizar(t, "Cancelled", motivo)
                if self.docking != "Docked":
                    self.docking = "Undocked"
                    self.modo = "stop"
                    self.estado_texto = self.extendido = "Stopped"
                self._evento("Stopped")
            else:
                self._actualizar(t, "Cancelled", motivo)
            hechos.append(t)
        return hechos

    def ejecutar_macro(self, nombre: str, pasos: list[tuple[str, ...]], template: bool) -> None:
        """executeMacro / executeMacroTemplate (con los $n ya sustituidos).
        Cada paso es (tarea, *argumentos), como lo lee `mapa.py`."""
        etiqueta = f"macro template {nombre}" if template else f"macro {nombre}"
        self._cortar_movimiento()
        self._interrumpir_trabajo()
        self.patrulla = None
        plan = self._pasos_de_macro(pasos, etiqueta)
        plan.append(Paso("fin_macro", macro=etiqueta))
        self._plan(*plan)
        self.estado_texto = self.extendido = f"Executing {etiqueta}"
        self._evento(f"Executing {etiqueta}")                            # visto en el LD

    def _pasos_de_macro(self, pasos: list[tuple[str, ...]], etiqueta: str, nivel: int = 0) -> list[Paso]:
        plan: list[Paso] = []
        for tarea, *args in pasos:
            valor = args[0] if args else ""
            if tarea == "goto":
                plan.append(Paso("goto", goal=valor, macro=etiqueta))
            elif tarea == "wait":
                plan.append(Paso("wait", segundos=float(valor or 0), macro=etiqueta))
            elif tarea == "gotoPoint":
                # visto en el LD (2026-11-20): `Task "gotoPoint" x y θ`, x e y en mm y θ en
                # grados, enteros; el robot acabó a pocos mm y con ese rumbo.
                try:
                    x, y = float(args[0]), float(args[1])
                    th = float(args[2]) if len(args) > 2 and args[2] != "" else None
                except (IndexError, ValueError):
                    continue        # supuesto: un punto mal escrito no hace nada (como un goal que no existe)
                plan.append(Paso("punto", punto=(x, y, th), macro=etiqueta))
            elif tarea == "MacroTask" and nivel < 5:
                # visto en el LD: una macro llama a otra (o a un template, con sus valores).
                # supuesto: sus pasos se hacen dentro de la de fuera, sin mensajes propios.
                otra, valores = valor, args[1:]
                sub = self.mapa.templates.get(otra)
                if sub is not None:
                    def sustituir(v: str) -> str:
                        return re.sub(r"\$(\d+)", lambda m: valores[int(m.group(1)) - 1]
                                      if int(m.group(1)) <= len(valores) else "", v)
                    sub = [tuple(sustituir(v) for v in p) for p in sub]
                else:
                    sub = self.mapa.macros.get(otra, [])
                plan += self._pasos_de_macro(sub, etiqueta, nivel + 1)
            # el resto (say, salidas…) es instantáneo y sin efecto en el digital
        return plan

    def _fallo_app(self, nombre: str) -> str:
        corto, largo = self.bloqueos[nombre]
        return f'Fault_Driving_Application {nombre} "{corto}" "{largo}" true false true false false'

    def bloquear(self, nombre: str, corto: str, largo: str) -> None:
        """visto en el LD: no pausa. Es un fallo de la aplicación que corta el
        movimiento y devuelve el trabajo a la cola sin robot; al quitarlo, el
        robot lo vuelve a coger."""
        self.bloqueos[nombre] = (corto, largo)
        self._evento(f"Fault: {self._fallo_app(nombre)}")
        p = self.paso
        if p and p.tipo == "goto" and p.empezado:
            self.estado_texto = f"Interrupted: Going to {p.goal}"
        self._cortar_movimiento()
        self._evento(f'RobotFault: "{self.nombre}" {self._fallo_app(nombre)}')
        t = self.trabajo_en_curso
        if t:
            self._actualizar(t, "Interrupted", "None")
            t.robot = "None"
            self._actualizar(t, "Pending", "Interrupted")
        self.pasos = []
        self.patrulla = None

    def desbloquear(self, nombre: str) -> bool:
        if nombre not in self.bloqueos:
            return False
        fallo = self._fallo_app(nombre)
        del self.bloqueos[nombre]
        self._evento(f"FaultCleared: {fallo}")
        self._quedar_parado()
        self._evento(f'RobotFaultCleared: "{self.nombre}" {fallo}')
        return True

    # --- simulación ---

    def avanzar(self, dt: float) -> None:
        self.uptime += dt
        self.retenido = max(0.0, self.retenido - dt)
        self._bateria(dt)
        self._decidir()
        self._mover(dt)
        if self.moviendose:
            self.idle = 0.0
        else:
            self.idle += dt

    def _bateria(self, dt: float) -> None:
        if self.docking == "Docked":
            # visto en el LD: en Float se queda en 99,5–99,6
            self.soc = min(99.6, self.soc + self.carga * dt)
        else:
            gasto = self.gasto_marcha if self.moviendose else self.gasto_parado
            self.soc = max(0.0, self.soc - gasto * dt)
        self.carga_estado = self._estado_carga()

    def _puede_trabajar(self) -> bool:
        return (not self.parado_por_algo and not self.perdido and self.retenido <= 0
                and not self.cargando_sin_terminar())

    def _decidir(self) -> None:
        """Qué hace el robot cuando nadie le manda nada: coger el siguiente
        trabajo de la cola o irse a cargar (autodock)."""
        ocupado = bool(self.pasos) or self.patrulla is not None
        yendo_a_cargar = (len(self.pasos) == 1 and self.pasos[0].tipo == "dock"
                          and not self.pasos[0].forzado)
        pendientes = sorted((t for t in self.cola if t.estado in ("Pending", "Interrupted")),
                            key=lambda t: (-t.prioridad, t.orden))
        if pendientes and self._puede_trabajar() and (not ocupado or yendo_a_cargar):
            t = pendientes[0]
            if yendo_a_cargar:
                self.pasos = []
                if self.docking == "Docking":
                    self.docking = "Undocked"
            self._actualizar(t, "InProgress", "UnAllocated")     # visto en el LD
            self._plan(Paso("goto", goal=t.goal, trabajo=t))
            return
        if ocupado or self.docking in ("Docked", "Docking") or not self.autodock:
            return
        if self.parado_por_algo or self.perdido or self.trabajo_en_curso:
            return
        if self.soc < self.autodock_soc or self.idle > self.autodock_idle_min * 60:
            self.pasos = [Paso("dock", forzado=False)]

    def _mover(self, dt: float) -> None:
        p = self.paso
        if p is None:
            return
        if self.parado_por_algo:
            return
        if not p.empezado:
            p.empezado = True
            self._empezar(p)
            if self.paso is not p:          # falló al empezar
                return
        p.t += dt
        if p.tipo == "undock":
            if p.t >= 11.0:                 # visto en el LD: unos 11 s de maniobra
                # sale marcha atrás unos 0,6 m
                self._desplazar(-600)
                self.docking, self.forzado = "Undocked", "Unforced"
                self.carga_estado = self._estado_carga()
                self.estado_texto = self.extendido = "Undocked"
                self._evento_dock()                            # visto en el LD
                self._terminar_paso()
            return
        if p.tipo == "goto":
            g = self.mapa.goals[p.goal]
            if self._acercarse(g.x, g.y, dt):
                if p.macro is None:          # visto en el LD: dentro de una macro no hay "Arrived at"
                    self.estado_texto = f"Arrived at {p.goal}"   # visto en el LD
                    self.extendido = "Done driving"              # visto en el LD
                    self._evento(f"Arrived at {p.goal}")         # visto en el LD
                if p.trabajo:
                    self._actualizar(p.trabajo, "InProgress", "After")  # visto: tareas al llegar
                    self._actualizar(p.trabajo, "Completed")
                self._terminar_paso()
                if self.patrulla and not self.pasos:
                    self._siguiente_de_patrulla()
            return
        if p.tipo == "punto":
            x, y, th = p.punto
            if self._acercarse(x, y, dt):
                if th is not None:
                    self.th = (th + 180) % 360 - 180      # llega con el rumbo pedido
                self._terminar_paso()
            return
        if p.tipo == "wait":
            if p.t >= p.segundos:
                self._evento("WaitState: Waiting completed")              # visto en el LD
                self._terminar_paso()
            return
        if p.tipo == "fin_macro":
            self.estado_texto = self.extendido = f"Completed {p.macro}"  # visto en el LD
            self._evento(f"Completed {p.macro}")                         # visto en el LD
            self._terminar_paso()
            return
        if p.tipo == "dock":
            if not p.cerca:
                if self._acercarse(self.dock[0], self.dock[1], dt):
                    p.cerca, p.t = True, 0.0      # junto al cargador: lo busca y se acopla
                return
            if p.t < 5.0:                         # supuesto: unos segundos buscando y acoplando
                return
            if True:
                self.x, self.y, self.th = self.dock
                # visto en el LD: al tocar los contactos, Docking … Bulk y luego Docked … Bulk
                self.carga_estado = "Bulk" if self.soc < 90 else "Overcharge" if self.soc < 99 else "Float"
                self._evento_dock()
                self.docking = "Docked"
                self.forzado = "Forced" if p.forzado else "Unforced"
                self.carga_estado = self._estado_carga()
                self.modo = "dock"
                self._evento_dock()                            # supuesto (igual que al salir)
                self._terminar_paso()

    def _empezar(self, p: Paso) -> None:
        if p.tipo == "undock":
            self.docking = "Undocking"
            self.estado_texto = self.extendido = "Undocking"
            self._evento_dock()                                # visto: ChargeState aún del dock
            self.carga_estado = "Not"
            self._evento_dock()                                # visto: enseguida pasa a Not
        elif p.tipo == "wait":
            self._evento(f'WaitState: Waiting {p.segundos:g} seconds with status "Waiting"')  # visto
        elif p.tipo == "goto" and p.macro is not None and p.goal not in self.mapa.goals:
            # visto en el LD: con un goal que no existe la macro "termina" sin error
            self.pasos = [x for x in self.pasos if x.tipo == "fin_macro"]
        elif p.tipo == "punto":
            self.modo = "goal"
            if self.perdido:
                self._fallar(f"point {p.punto[0]:g} {p.punto[1]:g}")
                self.pasos = []
                self.patrulla = None
        elif p.tipo == "goto":
            self.modo = "goal"
            if self.perdido:
                self._fallar(p.goal)
                if p.trabajo:
                    p.trabajo.fallos += 1
                    self._actualizar(p.trabajo, "Failed", "Failed going to goal")
                self.pasos = []
                self.patrulla = None
                return
            if p.trabajo:
                self._actualizar(p.trabajo, "InProgress", "Allocated")  # visto en el LD
                self._actualizar(p.trabajo, "InProgress", "Driving")    # visto en el LD
            if p.macro is None:
                self.estado_texto = self.extendido = f"Going to {p.goal}"
                self._evento(f"Going to {p.goal}")             # visto en el LD
        elif p.tipo == "dock":
            self.docking = "Docking"
            self.estado_texto = self.extendido = "Docking"
            self.modo = "dock"
            self._evento_dock()                                # supuesto (igual que al salir)

    def _fallar(self, goal: str) -> None:
        # visto en el LD (yendo a un punto): evento "Error: Failed going to goal" y estos
        # textos en status. Con un goal con nombre, supuesto: igual cambiando "point x y".
        self.estado_texto = "Error: Failed going to goal"
        self.extendido = f"Failed to get to {goal}|Buffering: Failed going to |Failed going to goal"
        self._evento("Error: Failed going to goal")

    def _terminar_paso(self) -> None:
        if self.pasos:
            self.pasos.pop(0)

    def _desplazar(self, mm: float) -> None:
        r = math.radians(self.th)
        self.x += mm * math.cos(r)
        self.y += mm * math.sin(r)
        self.odo_mm += abs(mm)
        self.encoder[0] += mm * math.cos(r)
        self.encoder[1] += mm * math.sin(r)

    def _acercarse(self, x: float, y: float, dt: float) -> bool:
        """Gira hacia el destino y avanza en línea recta. True al llegar."""
        dx, dy = x - self.x, y - self.y
        dist = math.hypot(dx, dy)
        if dist < 1.0:
            return True
        rumbo = math.degrees(math.atan2(dy, dx))
        dif = (rumbo - self.th + 180) % 360 - 180
        if abs(dif) > 1.0:
            g = max(-self.giro * dt, min(self.giro * dt, dif))
            self.th = (self.th + g + 180) % 360 - 180
            self.odo_deg += abs(g)
            self.encoder[2] = (self.encoder[2] + g + 180) % 360 - 180
            if abs(dif) > 30:               # gira en el sitio antes de arrancar
                return False
        paso = min(dist, self.velocidad * dt)
        r = math.atan2(dy, dx)
        self.x += paso * math.cos(r)
        self.y += paso * math.sin(r)
        self.odo_mm += paso
        self.encoder[0] += paso * math.cos(r)
        self.encoder[1] += paso * math.sin(r)
        return paso >= dist

    # --- solo del digital (para probar casos que en el real cuestan) ---

    def pulsar_seta(self, pulsada: bool) -> None:
        """visto en el LD: la seta corta el movimiento (no lo pausa); al soltarla los
        motores siguen deshabilitados hasta `enableMotors`."""
        if pulsada and not self.estop:
            self._cortar_movimiento()
            self._interrumpir_trabajo()
            self.pasos = []
            self.patrulla = None
            self.estop, self.motores = True, False
            self.modo = "seta"
            self._evento("Motors disabled")          # visto en el LD, antes de "EStop pressed"
            self._evento("EStop pressed")
        elif not pulsada and self.estop:
            self.estop = False
            self.modo = "motores"
            self._evento("EStop relieved but motors still disabled")

    def habilitar_motores(self) -> None:
        self.motores = True
        self._evento("Motors enabled")               # visto en el LD
        self._quedar_parado()

    def perder_localizacion(self) -> None:
        self.score = 0.0
