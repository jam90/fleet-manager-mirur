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


@dataclass
class Telemetry:
    """Snapshot normalizado del robot. Sin campos de marca: lo que el core
    necesita para el `state` VDA y para el asignador, ya interpretado."""
    battery: float                                   # %
    pose: tuple[float, float, float] | None = None   # x, y, theta (rad) en el mapa
    map_id: str | None = None
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


class RobotDriver(Protocol):
    """Contrato que cumple cada marca. Todos los métodos de red son
    tolerantes: no lanzan, devuelven None/False y guardan `last_error`."""

    manufacturer: str            # segmento <manufacturer> del topic ("MiR")
    last_error: str | None       # motivo del último fallo de red, para state.errors

    def connect(self) -> bool:
        """Índices, handshake, ... Se reintenta cada tick mientras devuelva False."""

    def poll(self) -> Telemetry | None:
        """Telemetría actual. None = robot inalcanzable (motivo en `last_error`)."""

    def translate(self, action: Action) -> Job:
        """Pura, sin red. Lanza `OrderRejected` con el errorType VDA adecuado."""

    def execute(self, job: Job, priority: int = 0) -> str:
        """Lanza el job en el robot. Devuelve un `job_id` opaco para `job_status`."""

    def job_status(self, job_id: str) -> JobStatus | None:
        """Estado del job. None = no se pudo consultar (se reintenta)."""

    def cancel(self, job_id: str) -> None:
        """Aborta el job (H5, instantActions/cancelOrder)."""

    def charge_job(self) -> Job | None:
        """Job de auto-carga (H4). None = la marca no lo soporta."""

    def extra_state(self, s: State) -> None:
        """Gancho opcional: campos del `state` que el core no puede deducir."""
