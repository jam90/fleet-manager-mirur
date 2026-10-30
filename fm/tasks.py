"""Cola de tareas de `fleet/order` (late binding, docs/plan-cola.md).

Una `fleet/order` que algún robot de la flota puede hacer se guarda como
`Task` aunque ahora estén todos ocupados, y el `Dispatcher` se la da al
primer robot que quede libre. Así el trabajo va al robot que termina antes,
no al que se eligió al llegar la order. Cada robot sigue teniendo como
mucho UNA mission del FM: la cola es del FM, no del robot.

Ciclo de vida de una tarea:

    QUEUED ──► ASSIGNED ──► FINISHED | FAILED
       └──► CANCELLED | EXPIRED      (salió de la cola sin ejecutarse)

Este módulo solo guarda los datos. Quién se lleva cada tarea, y lo que se
publica por MQTT, lo decide `fm/fleet.py`.

**Dónde se guardan** (decisión 155): detrás de `TaskStore`, igual que los
robots van detrás de `RobotDriver`. El resto del FM no sabe si las tareas
están en RAM (`MemoryTaskStore`, esta fase) o en SQLite (fase 2). Para pasar
a otro almacén se escribe otra clase, sin tocar la asignación.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from fm.vda5050.order import Order

QUEUED = "QUEUED"
ASSIGNED = "ASSIGNED"
FINISHED = "FINISHED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"
REJECTED = "REJECTED"       # no se llegó a aceptar: no se guarda (`TaskStore.remove`)

ALIVE = frozenset({QUEUED, ASSIGNED})
# Terminadas sin llegar a hacerse: la misma order (mismo orderId y
# orderUpdateId) se puede volver a mandar. Una FINISHED repetida se ignora:
# volver a ejecutarla movería otra vez el material.
RETRYABLE = frozenset({FAILED, CANCELLED, EXPIRED})


@dataclass
class Task:
    order: Order
    action_type: str
    received_at: float                 # epoch (s); orden de llegada y caducidad
    status: str = QUEUED
    announced: bool = False            # ya se publicó `QUEUED` al emisor
    serial: str | None = None          # robot, desde ASSIGNED
    job_id: str | None = None          # id del driver (entrada de la mission_queue del MiR)
    assigned_at: float | None = None
    finished_at: float | None = None
    result: str = ""                   # motivo de FAILED / CANCELLED / EXPIRED
    waiting: str = ""                  # por qué no se ha asignado todavía (lo del asignador)

    @property
    def order_id(self) -> str:
        return self.order.orderId

    @property
    def update_id(self) -> int:
        return self.order.orderUpdateId

    @property
    def alive(self) -> bool:
        return self.status in ALIVE


class TaskStore(Protocol):
    """Dónde viven las tareas. El `Dispatcher` cambia los campos de la
    `Task` y llama a `save()`: el almacén decide cómo guardarlo."""

    def add(self, task: Task) -> None: ...

    def save(self, task: Task) -> None: ...

    def remove(self, task: Task) -> None:
        """Para una tarea que nunca se llegó a aceptar (REJECTED al llegar):
        no queda rastro, y el emisor puede reintentarla con el mismo id."""

    def alive(self) -> list[Task]:
        """QUEUED y ASSIGNED, por orden de llegada."""

    def find(self, order_id: str) -> Task | None:
        """La más reciente con ese `orderId` (viva o terminada)."""


@dataclass
class MemoryTaskStore:
    """Tareas en RAM: se pierden al reiniciar el FM (fase 1). De las
    terminadas se guardan las últimas `keep_done`, para reconocer una order
    repetida que llega tarde."""
    keep_done: int = 500
    _tasks: list[Task] = field(default_factory=list)

    def add(self, task: Task) -> None:
        self._tasks.append(task)

    def save(self, task: Task) -> None:
        # En RAM la `Task` ya es el dato; solo se poda el histórico.
        done = [t for t in self._tasks if not t.alive]
        for t in done[:max(0, len(done) - self.keep_done)]:
            self._tasks.remove(t)

    def remove(self, task: Task) -> None:
        if task in self._tasks:
            self._tasks.remove(task)

    def alive(self) -> list[Task]:
        return [t for t in self._tasks if t.alive]

    def find(self, order_id: str) -> Task | None:
        return next((t for t in reversed(self._tasks) if t.order_id == order_id), None)


def iso(epoch: float) -> str:
    """Epoch → ISO 8601 UTC con milisegundos, como el `timestamp` VDA."""
    return (datetime.fromtimestamp(epoch, timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
