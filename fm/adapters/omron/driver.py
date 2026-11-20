"""`OmronDriver`: un OMRON LD (o HD/MD) por ARCL, detrás del `RobotDriver`.

Tres formas de ejecutar una action (catálogo de `fleet.yaml`, decisión 197;
el bloque puede ir directo o dentro de `omron:`):

```yaml
actions:
  ir_a_omron:
    goal_input: destino            # queuePickup "<destino>": trabajo de la cola
  ir_y_esperar:
    template: MacroTemplateFM      # executeMacroTemplate MacroTemplateFM "<destino>" <espera_s>
    params: [destino, espera_s]    # en el orden de $1, $2…
    goal_params: [destino]         # se comprueban contra getGoals antes de mandar
    number_params: [espera_s]
  saludar:
    macro: Macro1                  # executeMacro Macro1
```

Cómo se sigue cada una (todo visto en el LD real, 2026-11-20):
- Trabajo de la cola: el `job_id` lo pone el FM; los `QueueUpdate:` que llegan
  dicen Pending → InProgress → Completed / Cancelled / Failed. Si se pierde
  alguno, `queueQuery jobid` (sin Local).
- Macro / template: no tienen id. `Executing macro (template) X` →
  `Completed macro (template) X`; si falla un goto dentro: `Error: Failed
  going to goal` y nunca llega el Completed. Un goal inexistente NO da error
  (la macro "termina" sin hacer nada): por eso se comprueban antes los goals.
- La carga la hace el autodock del robot (decisión 198): `charge_job()` = None.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Mapping

from fm.adapters.base import ActionInfo, Job, JobStatus, ParamInfo, RobotSpec, Telemetry
from fm.adapters.omron.client import ArclClient, ArclError
from fm.config import ConfigError
from fm.vda5050.order import Action, OrderRejected
from fm.vda5050.state import (E_INVALID_ORDER_ACTION, E_NO_ROUTE_TO_TARGET, E_VALIDATION_FAILURE,
                              I_ROBOT_STATE, Info, State)

log = logging.getLogger("fm.omron")

LD_SPEC = RobotSpec("OMRON LD", kinematics="DIFF", robot_class="CARRIER", max_load_kg=90.0,
                    max_speed=1.35, width=0.5, length=0.7, height=0.4,
                    description="OMRON LD por ARCL (fm/adapters/omron)")

# Puntuación de localización por debajo de la cual se da por perdido.
# Visto en el LD: 0 sin localizar; 0,4–0,97 localizado y trabajando.
MIN_LOCALIZACION = 0.05


@dataclass
class OmronAction:
    action_type: str
    tipo: str                                   # "goal" | "template" | "macro"
    nombre: str = ""                            # template o macro
    goal_input: str | None = None               # tipo "goal"
    params: list[str] = field(default_factory=list)
    goal_params: list[str] = field(default_factory=list)
    number_params: list[str] = field(default_factory=list)
    prioridad: int = 10


@dataclass
class OmronRobotConfig:
    serial: str
    host: str
    password: str
    port: int = 7171
    actions: dict[str, OmronAction] = field(default_factory=dict)


def _accion(serial: str, tipo_action: str, spec: Mapping) -> OmronAction:
    spec = spec.get("omron", spec) if isinstance(spec, Mapping) else {}
    donde = f"[{serial}] actions.{tipo_action}"
    if "goal_input" in spec:
        return OmronAction(tipo_action, "goal", goal_input=str(spec["goal_input"]),
                           prioridad=int(spec.get("priority", 10)))
    if "template" in spec:
        params = [str(p) for p in spec.get("params") or []]
        goals = [str(p) for p in spec.get("goal_params") or []]
        nums = [str(p) for p in spec.get("number_params") or []]
        sobran = [p for p in goals + nums if p not in params]
        if sobran:
            raise ConfigError(f"{donde}: {sobran} no están en 'params' {params}")
        return OmronAction(tipo_action, "template", nombre=str(spec["template"]), params=params,
                           goal_params=goals, number_params=nums)
    if "macro" in spec:
        return OmronAction(tipo_action, "macro", nombre=str(spec["macro"]))
    raise ConfigError(f"{donde}: un OMRON necesita 'goal_input', 'template' o 'macro' "
                      f"(ver fm/adapters/omron/driver.py)")


def parse_config(serial: str, raw: Mapping, defaults: Mapping, env: Mapping[str, str]) -> OmronRobotConfig:
    m = {**defaults, **raw}
    clave_env = serial.upper().replace("-", "_")
    host = env.get(f"OMRON_HOST_{clave_env}") or m.get("host")
    if not host:
        raise ConfigError(f"[{serial}] falta 'host' (o OMRON_HOST_{clave_env} en .env)")
    # La clave ARCL va en .env, nunca en fleet.yaml (el repo de la instalación se comparte)
    password = env.get(f"OMRON_PASSWORD_{clave_env}") or env.get("OMRON_PASSWORD")
    if not password:
        raise ConfigError(f"[{serial}] falta la clave ARCL: OMRON_PASSWORD (o OMRON_PASSWORD_{clave_env}) en .env")
    acciones = {a: _accion(serial, a, d) for a, d in (m.get("actions") or {}).items()}
    return OmronRobotConfig(serial, str(host), password, int(m.get("port", 7171)), acciones)


@dataclass
class _Trabajo:
    tipo: str               # "goal" | "template" | "macro"
    ref: str                # job_id ARCL (goal) o nombre de la macro/template
    estado: JobStatus = "WAITING"
    resultado: str | None = None
    propio: bool = True


class OmronDriver:
    manufacturer = "OMRON"

    def __init__(self, cfg: OmronRobotConfig, client: ArclClient | None = None):
        self.cfg = cfg
        self.serial = cfg.serial
        self.client = client or ArclClient(cfg.host, cfg.password, cfg.port)
        self.last_error: str | None = None
        self.goals: list[str] = []
        self.macros: list[str] = []
        self.mapa: str | None = None
        self.docking: dict[str, str] = {}
        self._trabajos: dict[str, _Trabajo] = {}
        self._n = 0
        self._recargar_mapa = True
        self._pausado = False
        self._ultimo_status: dict[str, str] = {}
        self._ticks = 0

    # --- conexión e índices ---

    def _asegurar(self) -> bool:
        if self.client.conectado:
            return True
        try:
            self.client.conectar()
            self._recargar_mapa = True
            log.info("[%s] ARCL conectado a %s:%d", self.serial, self.cfg.host, self.cfg.port)
            return True
        except (OSError, TimeoutError, ConnectionError) as e:
            self.last_error = f"ARCL {self.cfg.host}:{self.cfg.port}: {e}"
            return False

    def _leer_mapa(self) -> None:
        self.goals = self.client.get_goals()
        self.macros = self.client.get_macros()
        self.mapa = self.client.get_config_section_values("Files").get("Map")
        self.docking = self.client.get_config_section_values("Docking")
        self._recargar_mapa = False
        log.info("[%s] mapa %s: goals %s, macros %s", self.serial, self.mapa, self.goals, self.macros)

    def connect(self) -> bool:
        if not self._asegurar():
            return False
        try:
            self._leer_mapa()
            self.last_error = None
            return True
        except (ArclError, OSError, TimeoutError) as e:
            self.last_error = f"ARCL: {e}"
            return False

    # --- telemetría ---

    def poll(self) -> Telemetry | None:
        if not self._asegurar():
            return None
        try:
            self._procesar(self.client.leer_eventos())
            if self._recargar_mapa:
                self._leer_mapa()
            st = self.client.status()
            self._procesar(self.client.leer_eventos())
            self._reconciliar(st)
        except (ArclError, OSError, TimeoutError) as e:
            self.last_error = f"ARCL: {e}"
            return None
        self.last_error = None
        self._ultimo_status = st
        return self._telemetria(st)

    def _telemetria(self, st: dict[str, str]) -> Telemetry:
        estado = st.get("Status", "")
        humano = st.get("ExtendedStatusForHumans", "")
        try:
            x, y, th = (float(v) for v in st.get("Location", "").split()[:3])
            pose = (x / 1000, y / 1000, math.atan2(math.sin(math.radians(th)), math.cos(math.radians(th))))
        except ValueError:
            pose = None
        score = float(st.get("LocalizationScore") or 0)
        en_dock = estado.startswith("DockingState: Docked")
        carga = re.search(r"ChargeState: (\w+)", estado)
        seta = estado == "EStop pressed"
        sin_motores = "motors still disabled" in estado
        perdido = score < MIN_LOCALIZACION
        moviendo = estado.startswith(("Going to", "Executing macro", "DockingState: Docking",
                                      "DockingState: Undocking", "Patrolling"))
        nuestro = any(t.estado in ("WAITING", "RUNNING") for t in self._trabajos.values())
        motivo = ("seta pulsada" if seta else "motores sin rearmar tras la seta" if sin_motores
                  else "sin localizar" if perdido else "")
        return Telemetry(
            battery=float(st.get("StateOfCharge") or 0),
            pose=pose,
            map_id=self.mapa,
            driving=moviendo and not self._pausado,
            paused=self._pausado,
            charging=en_dock and bool(carga) and carga.group(1) != "Not",
            emergency_stop=seta,
            available=not motivo,
            unavailable_reason=motivo,
            # Moverse, ir al dock o salir de él sin que lo haya pedido el FM
            # (MobilePlanner, el PLC, el autodock…): ocupado por otro (decisión 16).
            foreign_busy=moviendo and not nuestro,
            information=[Info(I_ROBOT_STATE, "INFO", f"{estado} · {humano}" if humano != estado else estado)],
        )

    # --- seguimiento de trabajos por los mensajes del robot ---

    def _vivos(self, tipo: str | None = None):
        return [t for t in self._trabajos.values()
                if t.estado in ("WAITING", "RUNNING") and (tipo is None or t.tipo == tipo)]

    def _procesar(self, eventos: list[str]) -> None:
        for e in eventos:
            if e.startswith("Map changed"):
                self._recargar_mapa = True
            elif e.startswith("QueueUpdate:"):
                partes = e.split()
                if len(partes) >= 5 and (t := self._trabajos.get(partes[2])) is not None:
                    estado, sub = partes[4], partes[5] if len(partes) > 5 else ""
                    if estado in ("Pending", "InProgress", "Interrupted", "Cancelling"):
                        t.estado = "WAITING" if estado == "Pending" and sub == "None" else "RUNNING"
                    elif estado == "Completed":
                        t.estado = "FINISHED"
                    elif estado in ("Cancelled", "Failed"):
                        t.estado, t.resultado = "FAILED", f"{estado} {sub}".strip()
            elif m := re.match(r"Executing macro(?: template)? (.+)$", e):
                for t in self._vivos():
                    if t.tipo in ("macro", "template") and t.ref == m.group(1).strip():
                        t.estado = "RUNNING"
            elif m := re.match(r"Completed macro(?: template)? (.+)$", e):
                for t in self._vivos():
                    if t.tipo in ("macro", "template") and t.ref == m.group(1).strip():
                        t.estado = "FINISHED"
            elif e.startswith(("Error:", "EStop pressed", "Interrupted:")):
                # Una macro que falla no dice cuál: la que esté en marcha
                for t in self._vivos():
                    if t.tipo in ("macro", "template") and t.estado == "RUNNING":
                        t.estado, t.resultado = "FAILED", e

    def _reconciliar(self, st: dict[str, str]) -> None:
        """Por si se perdió algún mensaje (wifi, reconexión): lo que digan
        `status` y, cada pocos ticks, `queueQuery` de los trabajos de la cola."""
        self._ticks += 1
        if self._ticks % 5 == 0:
            for t in self._vivos("goal"):
                for l in self.client.queue_query(t.ref):
                    if l.startswith("QueueQuery:"):
                        self._procesar(["QueueUpdate:" + l[len("QueueQuery:"):]])
        estado = st.get("Status", "")
        for t in self._vivos():
            if t.tipo in ("macro", "template"):
                if re.match(rf"Completed macro(?: template)? {re.escape(t.ref)}$", estado):
                    t.estado = "FINISHED"
                elif t.estado == "RUNNING" and estado.startswith("Error:"):
                    t.estado, t.resultado = "FAILED", estado

    # --- orders ---

    def translate(self, action: Action) -> Job:
        a = self.cfg.actions.get(action.actionType)
        if a is None:
            raise OrderRejected(E_INVALID_ORDER_ACTION,
                                f"[{self.serial}] actionType '{action.actionType}' no soportado; "
                                f"soporta {sorted(self.cfg.actions)}")
        p = action.params()

        def goal(clave: str) -> str:
            if clave not in p:
                raise OrderRejected(E_VALIDATION_FAILURE, f"falta el parámetro '{clave}'")
            g = str(p[clave])
            # Sin índices aún no se puede comprobar; execute() lo vuelve a mirar
            if self.goals and g not in self.goals:
                raise OrderRejected(E_NO_ROUTE_TO_TARGET,
                                    f"[{self.serial}] no hay goal '{g}' en el mapa {self.mapa}")
            return g

        if a.tipo == "goal":
            return Job(action, f"goal '{p.get(a.goal_input)}'", {"tipo": "goal", "goal": goal(a.goal_input),
                                                                 "prioridad": a.prioridad})
        if a.tipo == "macro":
            return Job(action, f"macro '{a.nombre}'", {"tipo": "macro", "nombre": a.nombre})
        valores = []
        for k in a.params:
            if k in a.goal_params:
                v = goal(k)
            elif k not in p:
                raise OrderRejected(E_VALIDATION_FAILURE, f"falta el parámetro '{k}'")
            else:
                v = p[k]
            if k in a.number_params:
                try:
                    v = int(float(v)) if float(v).is_integer() else float(v)
                except (TypeError, ValueError):
                    raise OrderRejected(E_VALIDATION_FAILURE, f"'{k}' debe ser un número, no '{v}'")
                valores.append(str(v))
            else:
                # Entre comillas: un goal "Taller 2" llega entero como $1 (visto en el LD)
                valores.append(f'"{v}"')
        return Job(action, f"template '{a.nombre}' {' '.join(valores)}",
                   {"tipo": "template", "nombre": a.nombre, "valores": valores,
                    "goals": [p[k] for k in a.goal_params]})

    def execute(self, job: Job, priority: int = 0) -> str:
        pl = job.payload
        if not self._asegurar():
            raise ConnectionError(self.last_error or "sin conexión ARCL")
        for g in pl.get("goals", []) + ([pl["goal"]] if pl["tipo"] == "goal" else []):
            if self.goals and g not in self.goals:
                raise OrderRejected(E_NO_ROUTE_TO_TARGET, f"[{self.serial}] no hay goal '{g}' en el mapa {self.mapa}")
        self._n += 1
        if pl["tipo"] == "goal":
            # El id del trabajo lo pone el FM (sin espacios: ARCL los separa)
            ref = re.sub(r"\W", "_", job.tag or "") or f"FM{self._n}"
            ref = f"{ref}-{self._n}" if ref in self._trabajos else ref
            self.client.queue_pickup(pl["goal"], pl.get("prioridad", 10), ref)
            self._trabajos[ref] = _Trabajo("goal", ref)
            return ref
        if self._vivos("macro") or self._vivos("template"):
            raise OrderRejected(E_INVALID_ORDER_ACTION, f"[{self.serial}] ya hay una macro en marcha")
        if pl["tipo"] == "template":
            self.client.execute_macro_template(pl["nombre"], pl["valores"])
        else:
            self.client.execute_macro(pl["nombre"])
        job_id = f"{pl['tipo']}-{self._n}"
        # La respuesta ya es "Executing macro …": está en marcha
        self._trabajos[job_id] = _Trabajo(pl["tipo"], pl["nombre"], "RUNNING")
        return job_id

    def job_status(self, job_id: str) -> JobStatus | None:
        t = self._trabajos.get(job_id)
        return None if t is None else t.estado     # lo mantiene poll() con los mensajes del robot

    def job_result(self, job_id: str) -> str | None:
        t = self._trabajos.get(job_id)
        return t.resultado if t is not None else None

    def cancel(self, job_id: str) -> None:
        t = self._trabajos.get(job_id)
        if t is None or t.estado not in ("WAITING", "RUNNING"):
            return
        try:
            if t.tipo == "goal":
                self.client.queue_cancel(t.ref)    # sin Local: vale aunque esté sin robot
            else:
                self.client.stop()
                t.estado, t.resultado = "FAILED", "cancelado desde el FM"
        except (ArclError, OSError, TimeoutError) as e:
            log.warning("[%s] no se pudo cancelar %s: %s", self.serial, job_id, e)

    def pause(self) -> None:
        """Bloqueo de conducción de la aplicación. Ojo (visto en el LD): no es una
        pausa, interrumpe el trabajo y lo devuelve a la cola; al quitarlo, el
        robot lo retoma. Una macro en marcha, en cambio, se corta."""
        self.client.application_block_driving_set("fm", "Pausa del FM", "Pausado desde el fleet manager")
        self._pausado = True

    def resume(self) -> None:
        try:
            self.client.application_block_driving_clear("fm")
        except ArclError:
            pass                                    # no había bloqueo
        self._pausado = False

    def charge_job(self) -> Job | None:
        return None                                 # autodock del robot (decisión 198)

    def extra_state(self, s: State) -> None:
        """Nada: lo propio del OMRON va en Telemetry.information."""

    def web_url(self) -> str | None:
        return f"https://{self.cfg.host}/"           # SetNetGo

    def describe_robot(self) -> RobotSpec:
        return LD_SPEC

    def describe_actions(self) -> list[ActionInfo]:
        salida = []
        for nombre, a in sorted(self.cfg.actions.items()):
            if a.tipo == "goal":
                params = [ParamInfo(a.goal_input, "position", True, self.goals or None)]
                desc = "trabajo de la cola (queuePickup)"
            elif a.tipo == "template":
                params = [ParamInfo(k, "position" if k in a.goal_params else
                                    "number" if k in a.number_params else "text", True,
                                    self.goals or None if k in a.goal_params else None)
                          for k in a.params]
                desc = f"macro template '{a.nombre}'"
            else:
                params, desc = [], f"macro '{a.nombre}'"
            salida.append(ActionInfo(nombre, params, desc))
        return salida

    def autodock(self) -> str:
        """Configuración del autodock del robot (decisión 198), legible."""
        d = self.docking
        if not d:
            return "desconocido"
        if d.get("AutoDock") != "true":
            return "desactivado"
        return (f"activado: al {d.get('AutoDockStateOfCharge', '?')} %, o tras "
                f"{d.get('AutoDockIdleTime', '?')} min parado; "
                f"{'hasta terminar de cargar' if d.get('DockUntilDoneCharging') == 'true' else 'carga parcial'}")
