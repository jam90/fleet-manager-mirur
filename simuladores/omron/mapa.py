"""Lectura de un mapa de OMRON (fichero `.map`, formato `2D-Map-Ex4`).

El mapa de un OMRON es un fichero de texto que se edita con MobilePlanner.
Lleva, entre otras cosas:

- `Cairn: <tipo> <x> <y> <θ> "<desc>" ICON "<nombre>" [x1 y1 x2 y2]`: los
  objetos (goals, líneas prohibidas, sectores…). Coordenadas en mm, θ en
  grados. Las líneas y sectores llevan además sus dos esquinas.
- `RouteInfo: …`: las rutas, como una lista de pasos `GoalTask "goto" "<goal>"`.
- `LINES` … `DATA`: paredes como segmentos y la nube de puntos del láser.

Aquí solo se lee lo que el OMRON digital necesita para contestar como el
robot: objetos, rutas y paredes. No hay navegación por el mapa: el digital
va en línea recta (ver README).
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

# Tipos de objeto que ARCL trata como destino (salen en getGoals y valen
# para goto/queuePickup). Sacado de `mapObjectTypeList` del LD real: los
# de categoría GoalType.
TIPOS_GOAL = ("Goal", "GoalWithHeading", "DoorGoal", "StandbyGoal")


@dataclass
class ObjetoMapa:
    tipo: str
    nombre: str
    x: int                      # mm
    y: int                      # mm
    th: float                   # grados
    descripcion: str = ""
    esquinas: tuple[int, int, int, int] | None = None   # líneas y sectores

    @property
    def es_goal(self) -> bool:
        return self.tipo in TIPOS_GOAL


@dataclass
class Mapa:
    fichero: str                                 # nombre, sin carpeta: "aula_ejemplo.map"
    objetos: list[ObjetoMapa] = field(default_factory=list)
    rutas: dict[str, list[str]] = field(default_factory=dict)   # ruta → goals en orden
    # macro / template → pasos ("goto", goal) | ("wait", s) | ("say", texto)…
    # En un template, los valores pueden ser "$1", "$2"… (visto en el LD)
    macros: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    templates: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    lineas: list[tuple[int, int, int, int]] = field(default_factory=list)

    @property
    def goals(self) -> dict[str, ObjetoMapa]:
        return {o.nombre: o for o in self.objetos if o.es_goal}

    def objeto(self, nombre: str) -> ObjetoMapa | None:
        for o in self.objetos:
            if o.nombre == nombre:
                return o
        return None


def _cairn(texto: str) -> ObjetoMapa | None:
    t = shlex.split(texto)
    # t[0] = "Cairn:", t[1] = tipo, t[2..4] = x y θ, t[5] = desc, t[6] = ICON, t[7] = nombre
    if len(t) < 8:
        return None
    esquinas = None
    if len(t) >= 12:
        esquinas = tuple(int(float(v)) for v in t[8:12])
    return ObjetoMapa(tipo=t[1], nombre=t[7], x=int(float(t[2])), y=int(float(t[3])),
                      th=float(t[4]), descripcion=t[5], esquinas=esquinas)


def leer_mapa(ruta: str | Path) -> Mapa:
    ruta = Path(ruta)
    mapa = Mapa(fichero=ruta.name)
    seccion = None
    ruta_actual = None
    pasos_actuales: list | None = None
    # latin-1: MobilePlanner guarda los textos en la codificación de Windows
    for linea in ruta.read_text(encoding="latin-1").splitlines():
        s = linea.strip()
        if s in ("LINES", "DATA"):
            seccion = s
            continue
        if s.startswith("Cairn:"):
            obj = _cairn(s)
            if obj:
                mapa.objetos.append(obj)
        elif s.startswith("RouteInfo:"):
            # visto en el LD: rutas, macros y templates comparten formato:
            #   RouteInfo: Macro "Macro1" / MacroTemplate "X" / Route "R"
            #   RouteInfo: GoalTask "goto" "Taller 2"   ← paso
            #   RouteInfo: Task "wait" 5                ← paso
            #   RouteInfo: Task "gotoPoint" "$1" "$2" "$3"           ← paso con varios argumentos
            #   RouteInfo: MacroTask "MacroTemplateCoordenadas" "8513" "-6166" "-89"
            #                                           ← paso: otra macro o template, con sus valores
            #   RouteInfo: _goto … / _goalAfter …       ← internas, se ignoran
            # Un paso es una tupla (tarea, *argumentos): ("goto", "Taller 2"),
            # ("gotoPoint", "$1", "$2", "$3"), ("MacroTask", "<nombre>", "<v1>"…).
            t = shlex.split(s)
            clave = t[1] if len(t) > 1 else ""
            if clave in ("Route", "Macro", "MacroTemplate") and len(t) >= 3:
                ruta_actual = t[2] if clave == "Route" else None
                pasos_actuales = [] if clave != "Route" else None
                if clave == "Route":
                    mapa.rutas[ruta_actual] = []
                elif clave == "Macro":
                    mapa.macros[t[2]] = pasos_actuales
                else:
                    mapa.templates[t[2]] = pasos_actuales
            elif clave in ("GoalTask", "Task") and len(t) >= 3:
                if ruta_actual is not None and clave == "GoalTask":
                    mapa.rutas[ruta_actual].append(t[3] if len(t) > 3 else "")
                elif pasos_actuales is not None:
                    pasos_actuales.append((t[2], *(t[3:] or [""])))
            elif clave == "MacroTask" and len(t) >= 3:
                # visto en el LD: así guarda MobilePlanner una macro hecha desde un template
                if pasos_actuales is not None:
                    pasos_actuales.append(("MacroTask", *t[2:]))
            elif not clave.startswith("_"):
                ruta_actual = pasos_actuales = None     # GoalAfter, EveryBefore… cierran el bloque
        elif seccion == "LINES":
            partes = s.split()
            if len(partes) == 4:
                mapa.lineas.append(tuple(int(v) for v in partes))
    return mapa
