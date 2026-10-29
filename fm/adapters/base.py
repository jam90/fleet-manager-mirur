"""Frontera entre el core del FM y cada marca de robot: tipos normalizados y
el `Protocol` que debe cumplir un driver.

El core (MQTT, VDA 5050, asignador, ciclo de vida de orders) solo habla con
estos tipos. Un driver (`fm/adapters/<marca>/`) traduce en las dos direcciones:

    marca → core : `poll()` devuelve `Telemetry` (nunca el status crudo).
    core → marca : `translate(Action)` devuelve un `Job` que `execute()` lanza.

`RobotDriver` es un `typing.Protocol`: no hace falta heredar, basta con que
la clase tenga estos métodos (duck typing). Así el core no importa nada de
ninguna marca y añadir una consiste en escribir una carpeta nueva.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from fm.vda5050.order import Action
from fm.vda5050.state import Error, Info, State

# Igual que `actionStatus` VDA (subconjunto grueso): el core lo copia tal cual.
JobStatus = Literal["WAITING", "RUNNING", "FINISHED", "FAILED"]
# Job aún vivo (en cola o ejecutándose): cuenta como `busy` y se sigue consultando.
JOB_ALIVE: tuple[JobStatus, ...] = ("WAITING", "RUNNING")


@dataclass
class Telemetry:
    """Snapshot normalizado del robot. Sin campos de marca: lo que el core
    necesita para el `state` VDA y para el asignador, ya interpretado."""
    battery: float                                   # %
    pose: tuple[float, float, float] | None = None   # x, y, theta (rad) en el mapa
    map_id: str | None = None
    # Site (entorno de mapas; en MiR, la session) al que pertenece el mapa
    # activo. None = el driver no lo sabe: no se compara con el de la flota.
    site_id: str | None = None
    site_name: str | None = None
    velocity: tuple[float, float] | None = None      # vx (m/s), omega (rad/s); robot diferencial: vy = 0
    driving: bool = False
    paused: bool = False
    charging: bool | None = None       # None = el driver no lo sabe (el overlay decide)
    operating_mode: str = "AUTOMATIC"  # OPERATING_MODES de vda5050/state.py
    emergency_stop: bool = False
    available: bool = True             # puede aceptar orders (no Manual / Error / E-stop)
    foreign_busy: bool = False         # ejecuta algo que NO lanzó el FM (decisión 16)
    unavailable_reason: str = ""       # texto para el rechazo cuando available=False
    errors: list[Error] = field(default_factory=list)   # ya en formato VDA
    information: list[Info] = field(default_factory=list)


@dataclass
class Job:
    """Lo que el driver va a ejecutar para una `Action` VDA. `payload` es
    opaco para el core: el driver guarda ahí lo que necesite (GUIDs,
    parámetros, ...)."""
    action: Action
    label: str          # para el log: "mission 'coger'"
    payload: Any = None


@dataclass
class ParamInfo:
    """Un parámetro de una action, para formularios (UI) y `factsheet.agvActions`."""
    key: str
    kind: str = "text"                   # "position" | "text" | "number"
    required: bool = True
    choices: list[str] | None = None     # valores admitidos, si el driver los conoce
    # Solo kind "number": límites para validar y para el deslizador de la UI.
    min: float | None = None
    max: float | None = None
    step: float | None = None


@dataclass
class ActionInfo:
    action_type: str
    params: list[ParamInfo] = field(default_factory=list)
    description: str = ""                # p.ej. el nombre de la mission en MiR


@dataclass
class RobotSpec:
    """Ficha técnica de la marca para el `factsheet` VDA (§6.11). Unidades SI."""
    series_name: str                     # "MiR250"
    kinematics: str = "DIFF"             # DIFF | OMNI | THREEWHEEL
    robot_class: str = "CARRIER"         # FORKLIFT | CONVEYOR | TUGGER | CARRIER
    max_load_kg: float = 0.0
    localization_types: list[str] = field(default_factory=lambda: ["NATURAL"])
    navigation_types: list[str] = field(default_factory=lambda: ["AUTONOMOUS"])
    min_speed: float = 0.0               # m/s
    max_speed: float = 1.0
    max_acceleration: float = 0.5        # m/s²
    max_deceleration: float = 0.5
    width: float = 0.5                   # m
    length: float = 0.5
    height: float = 0.3
    description: str = ""


# --- Misiones de un robot, para la pestaña Robot (decisión 113) --------------
# Lo que el robot ejecuta, lo haya lanzado el FM o no. `status` usa las
# palabras de `actionStatus` VDA (WAITING, RUNNING, PAUSED, FINISHED, FAILED)
# o, si la marca dice algo que no encaja, su texto tal cual. Horas en ISO 8601 UTC.


@dataclass
class MissionStep:
    kind: str                       # tipo de paso en la marca ("move", "charging"…)
    status: str
    message: str = ""               # ya legible: "Position 'Punto espera 2' reached"
    started: str | None = None
    finished: str | None = None


@dataclass
class MissionRun:
    job_id: str                     # el mismo `job_id` opaco que devuelve `execute()`
    name: str
    status: str
    own: bool = False               # la lanzó este FM (desde que arrancó)
    message: str = ""               # cómo terminó, en palabras del robot
    ordered: str | None = None
    started: str | None = None
    finished: str | None = None
    parameters: list[tuple[str, str]] = field(default_factory=list)   # (nombre, valor legible)
    steps: list[MissionStep] = field(default_factory=list)            # solo en la que está en curso


@dataclass
class MissionsView:
    current: MissionRun | None = None
    pending: list[MissionRun] = field(default_factory=list)    # en cola, la próxima primero
    history: list[MissionRun] = field(default_factory=list)    # terminadas, la última primero
    # Reloj del robot menos reloj del FM, en s (None = no se sabe). Las horas
    # de arriba son las del robot tal cual; la UI las corrige con esto.
    clock_offset_s: float | None = None


class RobotDriver(Protocol):
    """Contrato que cumple cada marca. Todos los métodos de red son
    tolerantes: no lanzan, devuelven None/False y guardan `last_error`."""

    manufacturer: str            # segmento <manufacturer> del topic ("MiR")
    last_error: str | None       # motivo del último fallo de red, para state.errors

    def connect(self) -> bool:
        """Índices, handshake, ... Mientras devuelva False el core lo reintenta
        (cada `CONNECT_RETRY_S`, solo si `poll()` responde)."""

    def poll(self) -> Telemetry | None:
        """Telemetría actual. None = robot inalcanzable (motivo en `last_error`)."""

    def translate(self, action: Action) -> Job:
        """Pura, sin red. Lanza `OrderRejected` con el errorType VDA adecuado."""

    def execute(self, job: Job, priority: int = 0) -> str:
        """Lanza el job en el robot. Devuelve un `job_id` opaco para `job_status`."""

    def job_status(self, job_id: str) -> JobStatus | None:
        """Estado del job. None = no se pudo consultar (se reintenta)."""

    def job_result(self, job_id: str) -> str | None:
        """Gancho opcional: motivo legible con el que terminó el job (p.ej.
        "Aborted - User Request"). VDA solo tiene `FAILED`; esto va a
        `actionStates[].actionResult` y a la descripción del error, para
        distinguir una parada a mano de un fallo de verdad."""

    def cancel(self, job_id: str) -> None:
        """Aborta el job (H5, instantActions/cancelOrder)."""

    def pause(self) -> None:
        """instantAction `startPause`: detener el robot sin abortar el job.
        Lanza `NotImplementedError` si la marca no lo soporta."""

    def resume(self) -> None:
        """instantAction `stopPause`: reanudar tras `pause()`."""

    def charge_job(self) -> Job | None:
        """Job de auto-carga (H4). None = la marca no lo soporta."""

    def extra_state(self, s: State) -> None:
        """Gancho opcional: campos del `state` que el core no puede deducir."""

    def describe_actions(self) -> list[ActionInfo]:
        """Gancho opcional: actionTypes que acepta y sus parámetros (UI,
        factsheet). Sin él, el core usa solo los nombres de `actions:`."""

    def describe_robot(self) -> RobotSpec:
        """Gancho opcional: ficha técnica para el `factsheet`. Sin él, el
        core publica una genérica con el nombre del driver."""

    def reconfigure(self, other: "RobotDriver") -> None:
        """Gancho opcional: adoptar la config de `other`, un driver de la misma
        marca recién construido con el `fleet.yaml` nuevo (cambio de actions
        en caliente, fm/reload.py). Sin red: después el core vuelve a llamar
        a `connect()` desde el hilo de poll. Sin el gancho, cambiar las
        actions del robot pide reiniciar el FM."""

    def make_missions(self) -> "MissionsSource":
        """Gancho opcional: objeto para consultar las misiones del robot desde
        la web (pestaña Robot). Se llama desde el hilo de la web, nunca desde el
        bucle de 1 Hz: debe usar su propia conexión. Sin él, la pestaña no
        enseña misiones."""


class MissionsSource(Protocol):
    def view(self, history: int = 10) -> MissionsView:
        """Misión en curso con sus pasos, cola e historial. Puede lanzar: quien
        llama (la web) convierte la excepción en un aviso."""


# --- Ganchos opcionales ------------------------------------------------------
# Un driver puede no implementarlos: el core siempre los llama a través de
# estas funciones, que ponen el valor por defecto en un único sitio.


def job_result(driver: RobotDriver, job_id: str) -> str | None:
    """Motivo del final del job si el driver lo sabe (gancho opcional)."""
    fn = getattr(driver, "job_result", None)
    return fn(job_id) if fn is not None else None


def describe_actions(driver: RobotDriver, action_types) -> list[ActionInfo]:
    """actionTypes del robot con sus parámetros (UI y factsheet). Sin el
    gancho, solo los nombres de `actions:` de fleet.yaml, sin parámetros."""
    fn = getattr(driver, "describe_actions", None)
    return fn() if fn is not None else [ActionInfo(t) for t in sorted(action_types or ())]
