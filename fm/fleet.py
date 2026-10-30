"""Despacho de mensajes MQTT entrantes: `<serial>/order`, `fleet/order`
(asignador con cola) e `instantActions`. Corre en el hilo principal,
drenando `bus.inbox` al inicio de cada tick.

`fleet/order` con cola (decisión 154, docs/plan-cola.md): una order que
algún robot puede hacer se guarda como tarea (`fm/tasks.py`) aunque estén
todos ocupados, y `tick()` se la da al primer robot que quede libre. Las
tareas se reparten por orden de llegada; una que espera a un robot concreto
no bloquea a las de detrás. `<serial>/order` no pasa por la cola: o se
acepta o se rechaza, como siempre.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

from fm.assigner import assign
from fm.config import FleetConfig
from fm.mqtt_bus import FLEET, MqttBus
from fm.instant import apply_instant_actions
from fm.orders import IgnoreOrder, pick_action
from fm.robot import Robot
from fm.tasks import (ASSIGNED, ASSIGNING, CANCELLED, EXPIRED, FAILED, FINISHED, QUEUED, REJECTED,
                      RETRYABLE, MemoryTaskStore, Task, TaskStore, iso)
from fm.vda5050.header import parse_topic, validate_header
from fm.vda5050.order import Action, Order, OrderRejected, parse_instant_actions, parse_order
from fm.vda5050.schemas import validation_errors
from fm.vda5050.state import (E_INVALID_INSTANT_ACTION, E_NO_MOBILE_ROBOT_AVAILABLE, E_NO_ORDER_TO_CANCEL,
                              E_OUTDATED_ORDER_UPDATE, E_QUEUE_FULL, E_VALIDATION_FAILURE, error_for_order)

log = logging.getLogger("fm.fleet")

SCHEMA_PROBLEMS_SHOWN = 3     # el motivo de rechazo cabe en un log y en la UI
# Cada cuánto se vuelve a buscar en el robot una tarea en duda tras reiniciar
# el FM (decisión 162): mientras el robot no conteste, no se puede saber.
DOUBT_RETRY_S = 30.0


def schema_problem(subtopic: str, data) -> str | None:
    """Motivo legible si `data` no cumple el schema de `subtopic`; None si valida."""
    problems = validation_errors(subtopic, data)
    if not problems:
        return None
    more = len(problems) - SCHEMA_PROBLEMS_SHOWN
    return ("no cumple el schema VDA 5050: " + "; ".join(problems[:SCHEMA_PROBLEMS_SHOWN])
            + (f" (y {more} más)" if more > 0 else ""))


class Dispatcher:
    def __init__(self, cfg: FleetConfig, robots: dict[str, Robot], bus: MqttBus, publish_state,
                 publish_factsheet=None, validate: bool = False, tasks: TaskStore | None = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.robots = robots
        self.bus = bus
        self.publish_state = publish_state      # callback(robot) → publica state ya (§6.6 eventos)
        self.publish_factsheet = publish_factsheet   # callback(robot) → factsheet retained (H5 factsheetRequest)
        self.tasks = tasks if tasks is not None else MemoryTaskStore()
        self.clock = clock                      # epoch: llegada y caducidad de las tareas
        self._last_queue: list[dict] | None = None   # último `fleet/queue` publicado (None = ninguno aún)
        # `--validate` (decisión 122): además de lo que comprueba `parse_order`,
        # cada order/instantActions entrante se valida contra el JSON Schema
        # oficial antes de procesarlo.
        self.validate = validate

    def subscribe(self) -> None:
        # Cada robot vive bajo el manufacturer de su driver: se escucha a todos
        # y `handle()` filtra por (manufacturer, serial) configurados.
        self.bus.subscribe("vda5050/v3/+/+/order")
        self.bus.subscribe("vda5050/v3/+/+/instantActions")

    def drain(self) -> None:
        while not self.bus.inbox.empty():
            topic, payload = self.bus.inbox.get_nowait()
            try:
                self.handle(topic, payload)
            except Exception:
                log.exception("[fleet] error procesando %s", topic)

    # ------------------------------------------------------------- despacho
    def handle(self, topic: str, payload: bytes) -> None:
        pt = parse_topic(topic)
        if pt is None:
            return
        if not self.bus.is_known(pt.manufacturer, pt.serial):
            log.debug("[fleet] %s ignorado: (%s, %s) no es un robot de esta flota",
                      topic, pt.manufacturer, pt.serial)
            return
        try:
            data = json.loads(payload)
        except ValueError:
            data = None
        invalid = validate_header(topic, data) if data is not None else "JSON malformado"
        if invalid is None and self.validate and pt.subtopic in ("order", "instantActions"):
            invalid = schema_problem(pt.subtopic, data)

        if pt.subtopic == "order" and pt.serial == FLEET:
            self.handle_fleet_order(data, invalid)
        elif pt.subtopic == "order":
            self.handle_robot_order(pt.serial, data, invalid)
        elif pt.subtopic == "instantActions" and pt.serial == FLEET:
            self.handle_fleet_instant_actions(data, invalid)
        elif pt.subtopic == "instantActions":
            self.handle_instant_actions(pt.serial, data, invalid)

    def handle_robot_order(self, serial: str, data, invalid: str | None) -> None:
        robot = self.robots.get(serial)
        if robot is None:
            log.warning("[fleet] order para robot desconocido '%s'", serial)
            return
        order_id = data.get("orderId") if isinstance(data, dict) else None
        try:
            if invalid:
                raise OrderRejected(E_VALIDATION_FAILURE, invalid)
            order = parse_order(data)
            req = robot.translate_order(order)
            robot.execute(order, req)
        except IgnoreOrder:
            log.info("[%s] order %s repetida: ignorada", serial, order_id)
            return
        except OrderRejected as e:
            robot.orders.reject(order_id, e)
        self.publish_state(robot)

    def handle_instant_actions(self, serial: str, data, invalid: str | None) -> None:
        robot = self.robots.get(serial)
        if robot is None:
            log.warning("[fleet] instantActions para robot desconocido '%s'", serial)
            return
        try:
            if invalid:
                raise OrderRejected(E_VALIDATION_FAILURE, invalid)
            actions = parse_instant_actions(data)
        except OrderRejected as e:
            # Mensaje inválido: no hay actionId al que referirse; error suelto en state.
            robot.orders.instant_errors = [error_for_order(e.error_type, e.description, level="WARNING")]
            log.warning("[%s] instantActions rechazadas: %s", serial, e)
            self.publish_state(robot)
            return
        apply_instant_actions(robot, actions, self.publish_state, self.publish_factsheet)

    # ------------------------------------------------------------ fleet/order
    def handle_fleet_order(self, data, invalid: str | None) -> None:
        order_id = data.get("orderId") if isinstance(data, dict) else None
        update_id = data.get("orderUpdateId") if isinstance(data, dict) else None
        try:
            if invalid:
                raise OrderRejected(E_VALIDATION_FAILURE, invalid)
            task = self._accept(parse_order(data), data)
        except OrderRejected as e:
            log.warning("[fleet] fleet/order %s rechazada: %s", order_id, e)
            self._respond(order_id or "", update_id if isinstance(update_id, int) else 0, REJECTED,
                          errorType=e.error_type, errorDescription=e.description)
            return
        if task is None:
            return
        # La nueva entra por detrás: si hay un robot libre, se lo lleva la
        # primera tarea que pueda hacerlo, no necesariamente esta.
        self._dispatch()
        if task.status == QUEUED:
            self._announce(task)
        self._publish_queue()

    def _accept(self, order: Order, raw: dict) -> Task | None:
        """Valida la order y la guarda como tarea QUEUED, o actualiza la que
        ya espera con ese `orderId`. None = repetida: no se hace nada. Lanza
        `OrderRejected` si no se acepta."""
        prev = self.tasks.find(order.orderId)
        if prev is not None and prev.status not in RETRYABLE:
            # Mismas reglas de orderUpdateId que una order dirigida (§6.1.4).
            if order.orderUpdateId == prev.update_id:
                log.info("[fleet] fleet/order %s/%d repetida: ignorada", order.orderId, order.orderUpdateId)
                return None
            if order.orderUpdateId < prev.update_id:
                raise OrderRejected(E_OUTDATED_ORDER_UPDATE,
                                    f"orderUpdateId {order.orderUpdateId} < {prev.update_id}, el último recibido")
            if prev.status in (ASSIGNING, ASSIGNED):
                raise OrderRejected(E_VALIDATION_FAILURE,
                                    f"la order '{order.orderId}' está en curso en {prev.serial}: "
                                    "v1 no admite ampliar una order en curso")
        action = pick_action(order)
        self._check_feasible(action)
        if prev is not None and prev.status == QUEUED:
            # Actualización de una que todavía espera: conserva su sitio.
            prev.order, prev.raw, prev.action_type, prev.announced = order, raw, action.actionType, False
            self.tasks.save(prev)
            return prev
        waiting = sum(1 for t in self.tasks.alive() if t.status == QUEUED)
        if waiting >= self.cfg.queue.max_size:
            raise OrderRejected(E_QUEUE_FULL, f"la cola está llena ({waiting} tareas esperando)")
        task = Task(order, action.actionType, self.clock(), raw=raw)
        self.tasks.add(task)
        return task

    def _check_feasible(self, action: Action) -> None:
        """¿Hay algún robot de la flota que pueda hacerla, aunque ahora esté
        ocupado? Si no, se rechaza ya: en la cola esperaría para siempre.
        Traducir no toca la red ni el robot."""
        rejections: dict[str, str] = {}
        failures: dict[str, OrderRejected] = {}
        for serial in sorted(self.robots):
            r = self.robots[serial]
            if action.actionType not in r.cfg.action_types:
                rejections[serial] = f"no soporta '{action.actionType}'"
                continue
            try:
                r.driver.translate(action)
                return
            except OrderRejected as e:
                # VALIDATION_FAILURE es culpa de la order (falta un parámetro,
                # no es un número…): ningún otro robot la aceptaría.
                if e.error_type == E_VALIDATION_FAILURE:
                    raise
                failures[serial] = e
        raise self._all_failed(rejections, failures)

    def tick(self) -> None:
        """Cada tick, con la telemetría recién leída: seguir las tareas en
        curso, caducar las que llevan demasiado esperando y repartir la cola."""
        self._follow_assigned()
        self._retry_doubts()
        self._expire()
        self._dispatch()
        self._publish_queue()

    def _dispatch(self) -> None:
        """Reparte las tareas QUEUED, por orden de llegada (decisión 156:
        sin prioridades), entre los robots libres. Se miran todas, no solo
        la primera: si la primera espera a un robot concreto, las de detrás
        pueden ir a otro."""
        for task in [t for t in self.tasks.alive() if t.status == QUEUED]:
            self._try_assign(task)

    def _try_assign(self, task: Task) -> None:
        snaps = {s: r.snapshot() for s, r in self.robots.items()}
        result = assign(task.action_type, snaps, self.cfg)
        if not result.ok:
            self._wait(task, result.reason)
            return
        # El asignador solo mira batería y ocupación; el robot elegido aún
        # puede rechazar al traducir (p.ej. tiene activo otro mapa). En ese
        # caso se prueba el siguiente del ranking (decisión 89). Traducir no
        # toca la red ni el robot, así que probar varios no tiene efectos.
        failures: dict[str, OrderRejected] = {}
        for serial in result.ranking:
            robot = self.robots[serial]
            try:
                job = robot.translate_order(task.order)
            except IgnoreOrder:
                failures[serial] = OrderRejected(E_NO_MOBILE_ROBOT_AVAILABLE, "ya ha recibido esta order")
                continue
            except OrderRejected as e:
                if e.error_type == E_VALIDATION_FAILURE:
                    self._drop(task, e)
                    return
                failures[serial] = e
                continue
            # ASSIGNING se guarda ANTES del POST (decisión 161): si el FM se
            # cae entre el POST y la respuesta, al arrancar busca la tarea en
            # el robot por su etiqueta, en vez de olvidarla o lanzarla dos veces.
            task.status, task.serial, task.assigned_at, task.waiting = ASSIGNING, serial, self.clock(), ""
            self.tasks.save(task)
            job.tag = task.tag
            # `execute` NO se reintenta en otro robot: tras un timeout el MiR
            # pudo encolarla igual y acabarían dos robots con la misma order.
            try:
                job_id = robot.execute(task.order, job)
            except OrderRejected as e:
                self._drop(task, e)
                return
            task.status, task.job_id = ASSIGNED, job_id
            self.tasks.save(task)
            self._respond(task.order_id, task.update_id, ASSIGNED,
                          assignedSerial=serial, description=f"asignado a {serial}")
            log.info("[fleet] fleet/order %s → %s", task.order_id, serial)
            self.publish_state(robot)
            return
        self._wait(task, self._all_failed(result.rejections, failures).description)

    def _wait(self, task: Task, reason: str) -> None:
        """Sigue en la cola. El motivo solo va al log cuando cambia: se
        evalúa en cada tick."""
        if reason != task.waiting:
            log.info("[fleet] fleet/order %s espera: %s", task.order_id, reason)
            task.waiting = reason
            self.tasks.save(task)

    def _announce(self, task: Task) -> None:
        queued = [t for t in self.tasks.alive() if t.status == QUEUED]
        task.announced = True
        self.tasks.save(task)
        self._respond(task.order_id, task.update_id, QUEUED,
                      queuePosition=queued.index(task) + 1, description=task.waiting)

    def _drop(self, task: Task, e: OrderRejected) -> None:
        """No se puede hacer. Si el emisor aún no sabía nada de ella (llegó y
        falló en el acto), es un REJECTED como antes de la cola, y puede
        reintentarla con el mismo orderId. Si ya estaba en la cola, FAILED."""
        if task.announced:
            self._close(task, FAILED, e.description, errorType=e.error_type, errorDescription=e.description)
            return
        task.status = REJECTED
        self.tasks.remove(task)
        log.warning("[fleet] fleet/order %s rechazada: %s", task.order_id, e)
        self._respond(task.order_id, task.update_id, REJECTED,
                      errorType=e.error_type, errorDescription=e.description)

    def _close(self, task: Task, status: str, reason: str, **fields) -> None:
        """La tarea sale de la cola sin ejecutarse (FAILED, CANCELLED, EXPIRED)."""
        task.status, task.result, task.finished_at = status, reason, self.clock()
        self.tasks.save(task)
        log.warning("[fleet] fleet/order %s %s: %s", task.order_id, status, reason)
        self._respond(task.order_id, task.update_id, status, **({"description": reason} | fields))

    def _follow_assigned(self) -> None:
        """Cierra las tareas cuyo robot ya terminó la order. No se publica:
        el resultado de la ejecución está en el `state` del robot."""
        for task in [t for t in self.tasks.alive() if t.status == ASSIGNED]:
            robot = self.robots.get(task.serial)
            if robot is None:
                continue        # su robot no corre en este FM (`--robot`): se deja como está
            a = robot.orders.active
            if a is not None and a.job_id == task.job_id and a.order.orderId == task.order_id:
                if a.alive:
                    continue
                status, result = (FINISHED if a.job_status == "FINISHED" else FAILED), a.result or ""
            else:
                # Otra order ocupó su sitio sin que la viéramos terminar; no
                # debería pasar (el robot solo acepta otra cuando está libre).
                status, result = FAILED, "el FM perdió su seguimiento"
            task.status, task.result, task.finished_at = status, result, self.clock()
            self.tasks.save(task)
            log.info("[fleet] fleet/order %s en %s: %s %s", task.order_id, task.serial, status, result)
            # El final se publica YA, antes de que `_dispatch` le dé la tarea
            # siguiente en este mismo tick: si no, el `state` del tick sale con
            # la order nueva y el FINISHED de esta no se ve nunca (la API REST
            # la dejaba en RUNNING; decisión 165).
            self.publish_state(robot)

    def _expire(self) -> None:
        limit = self.cfg.queue.max_wait_s
        if limit is None:
            return
        now = self.clock()
        for task in [t for t in self.tasks.alive() if t.status == QUEUED and now - t.received_at > limit]:
            self._close(task, EXPIRED, f"sin robot en {limit:.0f} s ({task.waiting or 'sin motivo'})")

    # ------------------------------------------------- al arrancar el FM
    def recover(self) -> None:
        """Una vez, al arrancar y con la telemetría ya leída: retoma lo que el
        FM anterior dejó a medias (decisiones 161–162). Las QUEUED siguen en
        la cola tal cual; el emisor ya tenía su `QUEUED`."""
        for task in self.tasks.alive():
            if task.status in (ASSIGNED, ASSIGNING) and task.serial not in self.robots \
                    and task.serial in self.cfg.robots:
                # Su robot está en la flota pero no corre en este FM (`--robot`):
                # no se toca; la retomará el FM que lo lleve.
                log.info("[fleet] fleet/order %s (%s en %s) no se retoma: el robot no corre en este FM",
                         task.order_id, task.status, task.serial)
            elif task.status == ASSIGNED:
                if self._readopt(task):
                    self._respond(task.order_id, task.update_id, ASSIGNED, assignedSerial=task.serial,
                                  description=f"asignado a {task.serial} (retomada tras reiniciar el FM)")
            elif task.status == ASSIGNING:
                self._resolve_assigning(task)
        # Se repite el QUEUED de las que esperan: quien siguiera la order (la
        # API REST del FM, cuyo historial vive en RAM, o el emisor) puede
        # haber perdido su estado. Para quien ya lo tenía no cambia nada.
        queued = [t for t in self.tasks.alive() if t.status == QUEUED]
        for pos, task in enumerate(queued, 1):
            self._respond(task.order_id, task.update_id, QUEUED, queuePosition=pos, description=task.waiting)
        if queued:
            log.info("[fleet] %d tareas en la cola desde antes del reinicio", len(queued))

    def _readopt(self, task: Task) -> bool:
        """ASSIGNED: el robot la estaba haciendo. Se vuelve a seguir su job
        como si el FM no se hubiera ido. False si no se puede (FAILED)."""
        robot = self.robots.get(task.serial)
        adopt = getattr(robot.driver, "adopt_job", None) if robot is not None else None
        if adopt is None:
            why = "ya no está en la flota" if robot is None else "tiene un driver que no permite retomarla"
            self._close(task, FAILED, f"el FM se reinició y {task.serial} {why}")
            return False
        try:
            job = robot.driver.translate(pick_action(task.order))
        except OrderRejected as e:
            self._close(task, FAILED, f"el FM se reinició y no puede retomarla: {e.description}")
            return False
        job.tag = task.tag
        adopt(task.job_id)
        robot.orders.accept(task.order, job, task.job_id)
        log.info("[fleet] fleet/order %s: retomada en %s (job=%s)", task.order_id, task.serial, task.job_id)
        return True

    def _resolve_assigning(self, task: Task) -> None:
        """ASSIGNING: el FM se cayó lanzándola. Se busca en el robot por su
        etiqueta: si la tiene, se sigue (ASSIGNED); si seguro que no, vuelve
        a la cola en su sitio; si no se puede saber, se queda en duda (D9) y
        se reintenta cada `DOUBT_RETRY_S`. Nunca se relanza a ciegas."""
        robot = self.robots.get(task.serial)
        find = getattr(robot.driver, "find_job", None) if robot is not None else None
        if robot is None or find is None:
            why = "ya no está en la flota" if robot is None else "tiene un driver que no sabe buscarla"
            self._doubt(task, f"{task.serial} {why}", retry=False)
            return
        if robot.last_telemetry is None:
            self._doubt(task, f"{task.serial} no responde")
            return
        try:
            job_id = find(task.tag)
        except Exception as e:                 # sin red, la marca no lo sabe…
            self._doubt(task, f"no se pudo buscar en {task.serial}: {type(e).__name__}: {str(e)[:120]}")
            return
        if job_id is None:
            serial = task.serial
            task.status, task.serial, task.assigned_at, task.retry_at = QUEUED, None, None, 0.0
            task.waiting = f"vuelve a la cola: el FM se reinició antes de que {serial} la recibiera"
            self.tasks.save(task)
            log.warning("[fleet] fleet/order %s: %s", task.order_id, task.waiting)
            if not task.announced:
                self._announce(task)
            return
        task.status, task.job_id, task.waiting, task.retry_at = ASSIGNED, job_id, "", 0.0
        self.tasks.save(task)
        if self._readopt(task):
            # El emisor puede no haber recibido el ASSIGNED: el FM se cayó antes.
            self._respond(task.order_id, task.update_id, ASSIGNED, assignedSerial=task.serial,
                          description=f"asignado a {task.serial} (comprobado tras reiniciar el FM)")

    def _doubt(self, task: Task, reason: str, retry: bool = True) -> None:
        task.retry_at = self.clock() + DOUBT_RETRY_S if retry else float("inf")
        waiting = f"en duda tras reiniciar el FM: {reason}"
        if waiting != task.waiting:
            log.warning("[fleet] fleet/order %s %s (cancelOrder la descarta)", task.order_id, waiting)
            task.waiting = waiting
            self.tasks.save(task)

    def _retry_doubts(self) -> None:
        now = self.clock()
        for task in [t for t in self.tasks.alive() if t.status == ASSIGNING and 0 < t.retry_at <= now]:
            self._resolve_assigning(task)

    # ------------------------------------------------- fleet/instantActions
    def handle_fleet_instant_actions(self, data, invalid: str | None) -> None:
        """Con el parámetro `orderId`. El resultado va a `order_response`,
        porque `fleet` no tiene `state`:

        - `cancelOrder` (decisión 156): si la tarea espera, sale de la cola;
          si está en un robot, es el `cancelOrder` de ese robot; si está en
          duda, se descarta. → `CANCELLED` o `CANCEL_FAILED`.
        - `requeueOrder` (propia, decisión 163): una tarea en duda tras un
          reinicio vuelve a la cola, en su sitio, porque alguien ha
          comprobado que el robot no la tiene. → `QUEUED` o `REQUEUE_FAILED`.
        """
        try:
            if invalid:
                raise OrderRejected(E_VALIDATION_FAILURE, invalid)
            actions = parse_instant_actions(data)
        except OrderRejected as e:
            log.warning("[fleet] fleet/instantActions rechazadas: %s", e)
            return
        for a in actions:
            if a.actionType == "cancelOrder":
                self._cancel(a)
            elif a.actionType == "requeueOrder":
                self._requeue(a)
            else:
                log.warning("[fleet] fleet/instantActions: '%s' no se admite (cancelOrder, requeueOrder)",
                            a.actionType)
        self._publish_queue()

    def _requeue(self, a: Action) -> None:
        order_id = next((p.value for p in a.actionParameters if p.key == "orderId"), None)
        task = self.tasks.find(order_id) if isinstance(order_id, str) else None
        if task is None or task.status != ASSIGNING:
            why = ("falta el parámetro 'orderId'" if not isinstance(order_id, str)
                   else f"la order '{order_id}' no está en duda: solo se relanza una que el FM "
                        "no sabe si llegó al robot")
            log.warning("[fleet] requeueOrder %s: %s", a.actionId, why)
            self._respond(order_id if isinstance(order_id, str) else "", task.update_id if task else 0,
                          "REQUEUE_FAILED", errorType=E_INVALID_INSTANT_ACTION, errorDescription=why)
            return
        # Es la palabra de quien lo ha comprobado contra la del FM: si el robot
        # sí la tenía, se hará dos veces. Por eso solo vale para una en duda.
        # Vuelve a contar desde ahora (conserva su puesto): el tiempo en duda
        # no la hace caducar nada más relanzarla, y el nuevo intento lleva
        # otra etiqueta (`Task.tag`), distinta de la del intento en duda.
        serial = task.serial
        task.status, task.serial, task.assigned_at, task.retry_at = QUEUED, None, None, 0.0
        task.received_at = self.clock()
        task.waiting = f"relanzada con requeueOrder ({a.actionId}): {serial} no la tenía"
        self.tasks.save(task)
        log.warning("[fleet] fleet/order %s: %s", task.order_id, task.waiting)
        self._announce(task)

    def _cancel(self, a: Action) -> None:
        order_id = next((p.value for p in a.actionParameters if p.key == "orderId"), None)
        task = self.tasks.find(order_id) if isinstance(order_id, str) else None
        robot = self.robots.get(task.serial) if task and task.serial else None
        active = robot.orders.active if robot else None
        if task is not None and task.status == ASSIGNED and not (
                active is not None and active.alive and active.job_id == task.job_id):
            task = None                       # ya terminó en el robot; `tick` la cerrará
        if task is None or not task.alive:
            why = (f"la order '{order_id}' no está en la cola ni en curso" if isinstance(order_id, str)
                   else "falta el parámetro 'orderId'")
            log.warning("[fleet] cancelOrder %s: %s", a.actionId, why)
            self._respond(order_id if isinstance(order_id, str) else "", 0, "CANCEL_FAILED",
                          errorType=E_NO_ORDER_TO_CANCEL, errorDescription=why)
            return
        if task.status == ASSIGNING:
            # En duda tras un reinicio (decisión 162): cancelarla es descartarla
            # en el FM; si el robot la llegó a recibir, allí sigue.
            self._close(task, CANCELLED, f"descartada con cancelOrder ({a.actionId}); "
                                         f"si {task.serial} la recibió, sigue en el robot")
            return
        if task.status == ASSIGNED:
            apply_instant_actions(robot, [Action("cancelOrder", a.actionId)],
                                  self.publish_state, self.publish_factsheet)
            if robot.orders.instant_states[-1].actionStatus != "FINISHED":
                e = robot.orders.instant_errors[-1]
                self._respond(task.order_id, task.update_id, "CANCEL_FAILED",
                              errorType=e.errorType, errorDescription=e.errorDescription)
                return
        self._close(task, CANCELLED, f"cancelada por cancelOrder ({a.actionId})")

    # ----------------------------------------------------------- publicación
    def _respond(self, order_id: str, update_id: int, status: str, **fields) -> None:
        self.bus.publish(FLEET, "order_response",
                         {"orderId": order_id, "orderUpdateId": update_id, "status": status, **fields})

    def _publish_queue(self) -> None:
        """`fleet/queue` retenido: las tareas que esperan, en orden (y las en
        duda tras un reinicio, `ASSIGNING`). Solo si cambia. El primero sale
        en el primer tick, aunque esté vacío: sustituye al del FM anterior."""
        body = [{"orderId": t.order_id, "orderUpdateId": t.update_id, "actionType": t.action_type,
                 "status": t.status, "receivedAt": iso(t.received_at), "waiting": t.waiting}
                | ({"serial": t.serial} if t.status == ASSIGNING else {})
                for t in self.tasks.alive() if t.status in (QUEUED, ASSIGNING)]
        if body == self._last_queue:
            return
        self._last_queue = body
        self.bus.publish(FLEET, "queue", {"tasks": body}, retain=True)

    @staticmethod
    def _all_failed(rejections: dict[str, str], failures: dict[str, OrderRejected]) -> OrderRejected:
        """Ningún candidato pudo traducir. Si todos fallaron por lo mismo se
        conserva ese errorType (con un solo candidato, el rechazo es el de
        siempre); si no, NO_MOBILE_ROBOT_AVAILABLE. La descripción lleva el
        motivo de cada robot, también de los que descartó el asignador."""
        types = {e.error_type for e in failures.values()}
        error_type = types.pop() if len(types) == 1 else E_NO_MOBILE_ROBOT_AVAILABLE
        reasons = {**rejections, **{s: e.description for s, e in failures.items()}}
        detail = "; ".join(f"{s}: {reasons[s]}" for s in sorted(reasons))
        return OrderRejected(error_type, f"ningún robot puede atender la order: {detail}")
