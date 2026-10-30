"""Cola de `fleet/order` (decisiones 154–157, docs/plan-cola.md): la tarea
va al primer robot que queda libre, por orden de llegada."""
import json

from fm.adapters.base import Telemetry
from fm.config import FleetConfig, QueueConfig, RobotConfig
from fm.fleet import Dispatcher
from fm.robot import Robot
from fm.tasks import CANCELLED, EXPIRED, FAILED, FINISHED, MemoryTaskStore
from fm.vda5050.header import HeaderCounter, make_header, topic
from test_fleet import FLEET_MAN, FakeBus, FakeDriver, _BrokenDriver


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class CancelDriver(FakeDriver):
    def __init__(self):
        super().__init__()
        self.cancelled: list[str] = []
        self.n = 0

    def execute(self, job, priority=0):
        self.executed.append(job)
        self.n += 1
        return str(self.n)

    def cancel(self, job_id):
        self.cancelled.append(job_id)


def _setup(actions={"mir-1": {"coger"}, "mir-2": {"coger"}}, queue=QueueConfig()):
    cfg = FleetConfig(robots={s: RobotConfig(s, action_types=frozenset(a)) for s, a in actions.items()},
                      queue=queue)
    robots = {}
    for s in actions:
        r = Robot(cfg.robots[s], CancelDriver())
        r.last_telemetry = Telemetry(battery=80.0)
        robots[s] = r
    bus = FakeBus({s: "MiR" for s in actions})
    clock = Clock()
    disp = Dispatcher(cfg, robots, bus, lambda r: None, clock=clock)
    return disp, robots, bus, clock


def _msg(subtopic, **body):
    return json.dumps({**make_header(HeaderCounter(), FLEET_MAN, "fleet", subtopic), **body})


def send(disp, order_id, action="coger", update_id=0):
    disp.handle(topic(FLEET_MAN, "fleet", "order"), _msg(
        "order", orderId=order_id, orderUpdateId=update_id, edges=[],
        nodes=[{"nodeId": "N0", "sequenceId": 0, "released": True,
                "actions": [{"actionType": action, "actionId": "a1", "blockingType": "HARD"}]}]))


def cancel(disp, order_id):
    params = [{"key": "orderId", "value": order_id}] if order_id else []
    disp.handle(topic(FLEET_MAN, "fleet", "instantActions"), _msg(
        "instantActions", actions=[{"actionType": "cancelOrder", "actionId": "c1",
                                    "blockingType": "HARD", "actionParameters": params}]))


def responses(bus, order_id=None):
    return [p for t, p in bus.published if t.endswith("/order_response")
            and (order_id is None or p["orderId"] == order_id)]


def last_queue(bus):
    return next(p for t, p in reversed(bus.published) if t.endswith("/fleet/queue"))["tasks"]


def finish(robot, status="FINISHED"):
    robot.orders.active.job_status = status


def test_todos_ocupados_queda_en_cola_y_va_al_primero_que_termina():
    disp, robots, bus, clock = _setup()
    send(disp, "o1"); send(disp, "o2")                      # uno a cada robot
    assert [r["assignedSerial"] for r in responses(bus)] == ["mir-1", "mir-2"]
    send(disp, "o3")
    r3 = responses(bus, "o3")[-1]
    assert r3["status"] == "QUEUED" and r3["queuePosition"] == 1
    assert "ocupado" in r3["description"]
    assert [t["orderId"] for t in last_queue(bus)] == ["o3"]
    disp.tick()                                             # nadie ha terminado
    assert responses(bus, "o3")[-1]["status"] == "QUEUED"
    finish(robots["mir-2"])                                 # mir-2 acaba antes
    disp.tick()
    r3 = responses(bus, "o3")[-1]
    assert r3["status"] == "ASSIGNED" and r3["assignedSerial"] == "mir-2"
    assert last_queue(bus) == []
    assert disp.tasks.find("o2").status == FINISHED


def test_orden_de_llegada():
    disp, robots, bus, clock = _setup()
    for o in ("o1", "o2", "o3", "o4"):
        send(disp, o)
    assert [r["queuePosition"] for r in responses(bus) if r["status"] == "QUEUED"] == [1, 2]
    finish(robots["mir-1"])
    disp.tick()
    assert responses(bus, "o3")[-1]["assignedSerial"] == "mir-1"
    assert responses(bus, "o4")[-1]["status"] == "QUEUED"


def test_la_primera_no_bloquea_a_las_de_detras():
    # o2 solo la puede hacer mir-1 (ocupado); o3 la puede hacer mir-2.
    disp, robots, bus, clock = _setup({"mir-1": {"coger", "dejar"}, "mir-2": {"coger"}})
    send(disp, "o1", "dejar")                               # mir-1 ocupado
    send(disp, "o2", "dejar")
    assert responses(bus, "o2")[-1]["status"] == "QUEUED"
    send(disp, "o3")
    assert responses(bus, "o3")[-1]["assignedSerial"] == "mir-2"


def test_la_nueva_no_se_cuela():
    # Si un robot queda libre, se lo lleva la que más lleva esperando.
    disp, robots, bus, clock = _setup({"mir-1": {"coger"}})
    send(disp, "o1"); send(disp, "o2")
    finish(robots["mir-1"])
    send(disp, "o3")
    assert responses(bus, "o2")[-1]["assignedSerial"] == "mir-1"
    r = responses(bus, "o3")[-1]
    assert r["status"] == "QUEUED" and r["queuePosition"] == 1


def test_repetida_en_cola_se_ignora_y_la_actualizacion_conserva_el_sitio():
    disp, robots, bus, clock = _setup({"mir-1": {"coger"}})
    send(disp, "o1"); send(disp, "o2"); send(disp, "o3")
    n = len(responses(bus))
    send(disp, "o2")                                        # QoS 1: llega dos veces
    assert len(responses(bus)) == n
    send(disp, "o2", update_id=1)
    r = responses(bus, "o2")[-1]
    assert r["status"] == "QUEUED" and r["orderUpdateId"] == 1 and r["queuePosition"] == 1
    send(disp, "o2", update_id=0)
    assert responses(bus, "o2")[-1]["errorType"] == "OUTDATED_ORDER_UPDATE"


def test_terminada_repetida_se_ignora():
    disp, robots, bus, clock = _setup({"mir-1": {"coger"}})
    send(disp, "o1")
    finish(robots["mir-1"])
    disp.tick()
    n = len(responses(bus))
    send(disp, "o1")
    assert len(responses(bus)) == n and len(robots["mir-1"].driver.executed) == 1


def test_ninguno_la_soporta_se_rechaza_sin_encolar():
    disp, robots, bus, clock = _setup()
    send(disp, "o1", "volar")
    r = responses(bus, "o1")[-1]
    assert r["status"] == "REJECTED" and "no soporta 'volar'" in r["errorDescription"]
    assert disp.tasks.alive() == []


def test_cola_llena():
    disp, robots, bus, clock = _setup({"mir-1": {"coger"}}, QueueConfig(max_size=2))
    for o in ("o1", "o2", "o3", "o4"):
        send(disp, o)
    r = responses(bus, "o4")[-1]
    assert r["status"] == "REJECTED" and r["errorType"] == "QUEUE_FULL"
    assert [t["orderId"] for t in last_queue(bus)] == ["o2", "o3"]


def test_caduca():
    disp, robots, bus, clock = _setup({"mir-1": {"coger"}}, QueueConfig(max_wait_s=60))
    send(disp, "o1"); send(disp, "o2")
    clock.t += 59; disp.tick()
    assert responses(bus, "o2")[-1]["status"] == "QUEUED"
    clock.t += 2; disp.tick()
    r = responses(bus, "o2")[-1]
    assert r["status"] == EXPIRED and "ocupado" in r["description"]
    assert last_queue(bus) == []
    send(disp, "o2")                                        # se puede volver a mandar
    assert responses(bus, "o2")[-1]["status"] == "QUEUED"


def test_fallo_al_lanzar_una_encolada_es_failed_y_no_prueba_otro():
    disp, robots, bus, clock = _setup()
    send(disp, "o1"); send(disp, "o2"); send(disp, "o3")
    robots["mir-1"].driver = _BrokenDriver(ConnectionError("timeout"))
    finish(robots["mir-1"])
    disp.tick()
    r = responses(bus, "o3")[-1]
    assert r["status"] == FAILED and r["errorType"] == "ROBOT_UNREACHABLE"
    assert len(robots["mir-2"].driver.executed) == 1        # solo o2


def test_cancelar_en_cola():
    disp, robots, bus, clock = _setup({"mir-1": {"coger"}})
    send(disp, "o1"); send(disp, "o2")
    cancel(disp, "o2")
    assert responses(bus, "o2")[-1]["status"] == CANCELLED
    assert last_queue(bus) == []
    finish(robots["mir-1"]); disp.tick()
    assert len(robots["mir-1"].driver.executed) == 1


def test_cancelar_en_curso_es_el_cancel_order_del_robot():
    disp, robots, bus, clock = _setup({"mir-1": {"coger"}})
    send(disp, "o1")
    cancel(disp, "o1")
    assert responses(bus, "o1")[-1]["status"] == CANCELLED
    assert robots["mir-1"].driver.cancelled == ["1"] and not robots["mir-1"].busy
    disp.tick()
    assert disp.tasks.find("o1").status == CANCELLED


def test_cancelar_lo_que_no_esta():
    disp, robots, bus, clock = _setup()
    cancel(disp, "nada")
    r = responses(bus)[-1]
    assert r["status"] == "CANCEL_FAILED" and r["errorType"] == "NO_ORDER_TO_CANCEL"
    cancel(disp, None)
    assert "orderId" in responses(bus)[-1]["errorDescription"]


def test_primer_tick_publica_la_cola_vacia_retenida():
    disp, robots, bus, clock = _setup()
    disp.tick()
    t, p = bus.published[-1]
    assert t == f"vda5050/v3/{FLEET_MAN}/fleet/queue" and p["tasks"] == []
    n = len(bus.published)
    disp.tick()                                             # sin cambios: no se repite
    assert len(bus.published) == n


def test_memory_store_poda_terminadas():
    from fm.tasks import Task
    from fm.vda5050.order import Order
    store = MemoryTaskStore(keep_done=2)
    for i in range(4):
        t = Task(Order(f"o{i}", 0, [], []), "coger", float(i), status=FINISHED)
        store.add(t); store.save(t)
    assert store.find("o0") is None and store.find("o3") is not None


def test_el_final_se_publica_antes_de_dar_la_siguiente():
    # Decisión 165: sin esto, el state del tick ya llevaba la order nueva y
    # el FINISHED de la anterior no se publicaba nunca.
    disp, robots, bus, clock = _setup({"mir-1": {"coger"}})
    seen = []
    disp.publish_state = lambda r: seen.append((r.orders.active.order.orderId, r.orders.active.job_status))
    send(disp, "o1"); send(disp, "o2")
    finish(robots["mir-1"])
    seen.clear()
    disp.tick()
    assert seen[:2] == [("o1", "FINISHED"), ("o2", "WAITING")]
