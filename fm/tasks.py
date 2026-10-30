"""Cola de tareas de `fleet/order` (late binding, docs/plan-cola.md).

Una `fleet/order` que algún robot de la flota puede hacer se guarda como
`Task` aunque ahora estén todos ocupados, y el `Dispatcher` se la da al
primer robot que quede libre. Así el trabajo va al robot que termina antes,
no al que se eligió al llegar la order. Cada robot sigue teniendo como
mucho UNA mission del FM: la cola es del FM, no del robot.

Ciclo de vida de una tarea:

    QUEUED ──► ASSIGNING ──► ASSIGNED ──► FINISHED | FAILED
       └──► CANCELLED | EXPIRED      (salió de la cola sin ejecutarse)

`ASSIGNING` (decisión 161) es el momento entre elegir robot y que el robot
conteste al POST. Se guarda ANTES de lanzar: si el FM se cae justo ahí, al
arrancar sabe que esa tarea está en duda y la busca en el robot en vez de
olvidarla o lanzarla otra vez.

Este módulo solo guarda los datos. Quién se lleva cada tarea, y lo que se
publica por MQTT, lo decide `fm/fleet.py`.

**Dónde se guardan** (decisión 155): detrás de `TaskStore`, igual que los
robots van detrás de `RobotDriver`. El resto del FM no sabe si las tareas
están en RAM (`MemoryTaskStore`: tests) o en SQLite (`SqliteTaskStore`: el
FM de verdad, decisión 160). Para pasar a otro almacén se escribe otra
clase, sin tocar la asignación.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from fm.vda5050.order import Order, parse_order

log = logging.getLogger("fm.tasks")

QUEUED = "QUEUED"
ASSIGNING = "ASSIGNING"
ASSIGNED = "ASSIGNED"
FINISHED = "FINISHED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"
REJECTED = "REJECTED"       # no se llegó a aceptar: no se guarda (`TaskStore.remove`)

ALIVE = frozenset({QUEUED, ASSIGNING, ASSIGNED})
# Terminadas sin llegar a hacerse: la misma order (mismo orderId y
# orderUpdateId) se puede volver a mandar. Una FINISHED repetida se ignora:
# volver a ejecutarla movería otra vez el material.
RETRYABLE = frozenset({FAILED, CANCELLED, EXPIRED})


@dataclass
class Task:
    order: Order
    action_type: str
    received_at: float                 # epoch (s): espera y caducidad; al relanzarla, cuándo se relanzó
    status: str = QUEUED
    announced: bool = False            # ya se publicó `QUEUED` al emisor
    serial: str | None = None          # robot, desde ASSIGNED
    job_id: str | None = None          # id del driver (entrada de la mission_queue del MiR)
    assigned_at: float | None = None
    finished_at: float | None = None
    result: str = ""                   # motivo de FAILED / CANCELLED / EXPIRED
    waiting: str = ""                  # por qué no se ha asignado todavía (lo del asignador)
    raw: dict = field(default_factory=dict)   # la order tal como llegó: se recarga de aquí
    id: int | None = None              # fila en SQLite (orden de llegada); None en RAM
    # Solo en RAM: cuándo volver a buscar en el robot una ASSIGNING en duda.
    retry_at: float = 0.0

    @property
    def order_id(self) -> str:
        return self.order.orderId

    @property
    def update_id(self) -> int:
        return self.order.orderUpdateId

    @property
    def alive(self) -> bool:
        return self.status in ALIVE

    @property
    def tag(self) -> str:
        """Etiqueta que se deja en el robot al lanzarla (`Job.tag`, decisión
        161). Lleva la hora de llegada: si la misma order se reintenta tras
        un FAILED, la entrada vieja del robot no se confunde con la nueva."""
        return f"{self.order_id}@{self.received_at:.3f}"


class TaskStore(Protocol):
    """Dónde viven las tareas. El `Dispatcher` cambia los campos de la
    `Task` y llama a `save()`: el almacén decide cómo guardarlo."""

    def add(self, task: Task) -> None: ...

    def save(self, task: Task) -> None: ...

    def remove(self, task: Task) -> None:
        """Para una tarea que nunca se llegó a aceptar (REJECTED al llegar):
        no queda rastro, y el emisor puede reintentarla con el mismo id."""

    def alive(self) -> list[Task]:
        """QUEUED, ASSIGNING y ASSIGNED, por orden de llegada."""

    def find(self, order_id: str) -> Task | None:
        """La más reciente con ese `orderId` (viva o terminada)."""


@dataclass
class MemoryTaskStore:
    """Tareas en RAM: se pierden al reiniciar el FM (para tests). De las
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


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,           -- orden de llegada
    order_id    TEXT    NOT NULL,
    update_id   INTEGER NOT NULL,
    action_type TEXT    NOT NULL,
    payload     TEXT    NOT NULL,              -- la order VDA tal como llegó, en JSON
    status      TEXT    NOT NULL,              -- QUEUED, ASSIGNING, ASSIGNED, FINISHED…
    announced   INTEGER NOT NULL DEFAULT 0,    -- ya se respondió QUEUED al emisor
    serial      TEXT,                          -- robot, desde ASSIGNING
    job_id      TEXT,                          -- id del driver (mission_queue del MiR)
    received_at REAL    NOT NULL,              -- epoch UTC (al relanzarla, cuándo se relanzó)
    assigned_at REAL,
    finished_at REAL,
    result      TEXT    NOT NULL DEFAULT '',   -- motivo de FAILED / CANCELLED / EXPIRED
    waiting     TEXT    NOT NULL DEFAULT ''    -- por qué no se ha asignado aún
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS tasks_order ON tasks(order_id);
"""

# Columnas que cambian a lo largo de la vida de una tarea (`save`).
_MUTABLE = ("update_id", "action_type", "payload", "status", "announced", "serial", "job_id",
            "received_at", "assigned_at", "finished_at", "result", "waiting")


class SqliteTaskStore:
    """Tareas en un fichero SQLite (decisión 160): sobreviven a un reinicio
    del FM, porque el emisor ya recibió `QUEUED` y no la va a reenviar.

    - Cada cambio es una transacción (`commit` en cada `add`/`save`): lo que
      el FM ya respondió está en disco aunque se vaya la luz justo después.
      Modo de diario por defecto de SQLite (no WAL): escribe poco y así un
      corte no se lleva el último cambio.
    - Un solo escritor: el hilo principal (el `Dispatcher`). La web no lee de
      aquí, lee el espejo MQTT (decisión 145); por eso no hace falta más.
    - Las vivas se guardan también en RAM (`_alive`) para no consultar el
      fichero en cada tick. El fichero manda: al arrancar se cargan de él.
    - Histórico: las terminadas se quedan; al abrir, se borran las de más de
      `keep_days` días (D8). None = no se borra nada.
    """

    def __init__(self, path: str | Path, keep_days: float | None = 30.0, now: float | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if keep_days is not None:
            limit = (now if now is not None else time.time()) - keep_days * 86400
            alive = tuple(ALIVE)
            n = self.db.execute(f"DELETE FROM tasks WHERE status NOT IN ({','.join('?' * len(alive))}) "
                                "AND finished_at < ?", (*alive, limit)).rowcount
            self.db.commit()
            if n:
                log.info("[tasks] %d tareas de más de %.0f días borradas del histórico", n, keep_days)
        alive = tuple(ALIVE)
        rows = self.db.execute(f"SELECT * FROM tasks WHERE status IN ({','.join('?' * len(alive))}) "
                               "ORDER BY id", alive)
        self._alive = [self._task(r) for r in rows]

    def add(self, task: Task) -> None:
        cur = self.db.execute(
            "INSERT INTO tasks (order_id, " + ", ".join(_MUTABLE) + ") VALUES "
            "(?, " + ", ".join("?" * len(_MUTABLE)) + ")",
            (task.order_id, *self._values(task)))
        self.db.commit()
        task.id = cur.lastrowid
        if task.alive:
            self._alive.append(task)

    def save(self, task: Task) -> None:
        self.db.execute("UPDATE tasks SET " + ", ".join(f"{c} = ?" for c in _MUTABLE) + " WHERE id = ?",
                        (*self._values(task), task.id))
        self.db.commit()
        if not task.alive and task in self._alive:
            self._alive.remove(task)

    def remove(self, task: Task) -> None:
        self.db.execute("DELETE FROM tasks WHERE id = ?", (task.id,))
        self.db.commit()
        if task in self._alive:
            self._alive.remove(task)

    def alive(self) -> list[Task]:
        return list(self._alive)

    def find(self, order_id: str) -> Task | None:
        row = self.db.execute("SELECT * FROM tasks WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                              (order_id,)).fetchone()
        if row is None:
            return None
        # Una viva se devuelve tal cual (el Dispatcher la cambia y la guarda);
        # una terminada, leída del fichero.
        return next((t for t in self._alive if t.id == row["id"]), None) or self._task(row)

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _values(t: Task) -> tuple:
        return (t.update_id, t.action_type, json.dumps(t.raw, ensure_ascii=False), t.status,
                int(t.announced), t.serial, t.job_id, t.received_at, t.assigned_at, t.finished_at,
                t.result, t.waiting)

    @staticmethod
    def _task(r: sqlite3.Row) -> Task:
        raw = json.loads(r["payload"])
        return Task(parse_order(raw), r["action_type"], r["received_at"], status=r["status"],
                    announced=bool(r["announced"]), serial=r["serial"], job_id=r["job_id"],
                    assigned_at=r["assigned_at"], finished_at=r["finished_at"], result=r["result"],
                    waiting=r["waiting"], raw=raw, id=r["id"])


def iso(epoch: float) -> str:
    """Epoch → ISO 8601 UTC con milisegundos, como el `timestamp` VDA."""
    return (datetime.fromtimestamp(epoch, timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
