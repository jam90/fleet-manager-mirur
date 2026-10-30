"""Cola de `fleet/order` con SQLite: se "mata" el FM en cada punto del ciclo
de vida y otro arranca sobre el mismo fichero (decisiones 160–162)."""
import pytest

from fm.adapters.base import Telemetry
from fm.config import FleetConfig, RobotConfig
from fm.fleet import DOUBT_RETRY_S, Dispatcher
from fm.robot import Robot
from fm.tasks import ASSIGNED, ASSIGNING, CANCELLED, FAILED, FINISHED, QUEUED, SqliteTaskStore
from test_fleet import FakeBus
import json

from fm.vda5050.header import HeaderCounter, make_header, topic
from test_queue import FLEET_MAN, CancelDriver, Clock, cancel, finish, last_queue, responses, send


class RecoverDriver(CancelDriver):
    """Driver que sabe retomar jobs y buscarlos por su etiqueta, como el MiR."""
    def __init__(self, robot_jobs=None, fail_find=False):
        super().__init__()
        self.robot_jobs = robot_jobs if robot_jobs is not None else {}   # tag → job_id: la cola del robot
        self.fail_find = fail_find
        self.adopted: list[str] = []
        self.tags: list[str | None] = []

    def execute(self, job, priority=0):
        self.tags.append(job.tag)
        job_id = super().execute(job, priority)
        self.robot_jobs[job.tag] = job_id
        return job_id

    def find_job(self, tag):
        if self.fail_find:
            raise ConnectionError("sin red")
        return self.robot_jobs.get(tag)

    def adopt_job(self, job_id):
        self.adopted.append(job_id)


class Crash(BaseException):
    """El FM muere en mitad del POST (no es un error que el FM gestione)."""


class CrashDriver(RecoverDriver):
    def __init__(self, receives: bool):
        super().__init__()
        self.receives = receives

    def execute(self, job, priority=0):
        if self.receives:                       # el robot la encola y luego cae el FM
            super().execute(job, priority)
        raise Crash()


SERIALS = ("mir-1", "mir-2")


class Fm:
    """Un FM sobre el fichero `db`, con sus robots. `drivers` sobrevive al
    reinicio (son los robots físicos), el resto no."""
    def __init__(self, db, drivers, clock, running=SERIALS):
        self.cfg = FleetConfig(robots={s: RobotConfig(s, action_types=frozenset({"coger"})) for s in SERIALS})
        self.robots = {}
        for s in running:
            r = Robot(self.cfg.robots[s], drivers[s])
            r.last_telemetry = Telemetry(battery=80.0)
            self.robots[s] = r
        self.bus = FakeBus({s: "MiR" for s in SERIALS})
        self.store = SqliteTaskStore(db)
        self.disp = Dispatcher(self.cfg, self.robots, self.bus, lambda r: None, tasks=self.store, clock=clock)

    def restart(self, drivers, clock, running=SERIALS):
        self.store.close()
        fm = Fm(self.store.path, drivers, clock, running)
        fm.disp.recover()
        return fm


@pytest.fixture
def world(tmp_path):
    drivers = {s: RecoverDriver() for s in SERIALS}
    clock = Clock()
    return Fm(tmp_path / "cola.sqlite", drivers, clock), drivers, clock


def test_las_que_esperan_siguen_en_la_cola(world):
    fm, drivers, clock = world
    send(fm.disp, "o1"); send(fm.disp, "o2"); send(fm.disp, "o3"); send(fm.disp, "o4")
    fm = fm.restart(drivers, clock)
    assert [t.order_id for t in fm.store.alive() if t.status == QUEUED] == ["o3", "o4"]
    # Al arrancar repite el QUEUED (con su puesto) y el ASSIGNED de las retomadas.
    assert [(r["orderId"], r["status"], r.get("queuePosition")) for r in responses(fm.bus)] == [
        ("o1", ASSIGNED, None), ("o2", ASSIGNED, None), ("o3", QUEUED, 1), ("o4", QUEUED, 2)]
    n = len(responses(fm.bus))
    send(fm.disp, "o3")                                  # repetida tras el reinicio: se ignora
    assert len(responses(fm.bus)) == n
    finish(fm.robots["mir-2"])
    fm.disp.tick()
    assert responses(fm.bus, "o3")[-1]["assignedSerial"] == "mir-2"
    assert [t["orderId"] for t in last_queue(fm.bus)] == ["o4"]


def test_las_que_estaban_en_curso_se_retoman(world):
    fm, drivers, clock = world
    send(fm.disp, "o1")
    fm = fm.restart(drivers, clock)
    r = fm.robots["mir-1"]
    assert drivers["mir-1"].adopted == ["1"] and r.busy and r.orders.active.order.orderId == "o1"
    finish(r)
    fm.disp.tick()
    assert fm.store.find("o1").status == FINISHED


def test_sin_adopt_job_no_se_puede_retomar(world):
    fm, drivers, clock = world
    send(fm.disp, "o1")
    plain = {s: CancelDriver() for s in SERIALS}         # driver sin los ganchos
    fm = fm.restart(plain, clock)
    r = responses(fm.bus, "o1")[-1]
    assert r["status"] == FAILED and "no permite retomarla" in r["description"]


def test_caida_lanzando_y_el_robot_la_tiene(world):
    fm, drivers, clock = world
    drivers["mir-1"] = CrashDriver(receives=True)
    fm.robots["mir-1"].driver = drivers["mir-1"]
    with pytest.raises(Crash):
        send(fm.disp, "o1")
    assert fm.store.find("o1").status == ASSIGNING      # guardada ANTES del POST
    fm = fm.restart(drivers, clock)
    r = responses(fm.bus, "o1")[-1]
    assert r["status"] == ASSIGNED and r["assignedSerial"] == "mir-1"
    assert fm.robots["mir-1"].orders.active.order.orderId == "o1"
    assert drivers["mir-1"].robot_jobs == {drivers["mir-1"].tags[0]: "1"}   # la etiqueta lleva el orderId
    assert drivers["mir-1"].tags[0].startswith("o1@")


def test_caida_lanzando_y_el_robot_no_la_tiene_vuelve_a_la_cola(world):
    fm, drivers, clock = world
    send(fm.disp, "o0")                                  # mir-1 ocupado: o1 irá a mir-2
    drivers["mir-2"] = CrashDriver(receives=False)
    fm.robots["mir-2"].driver = drivers["mir-2"]
    with pytest.raises(Crash):
        send(fm.disp, "o1")
    drivers["mir-2"] = RecoverDriver()                   # el robot no tiene nada con su etiqueta
    fm = fm.restart(drivers, clock)
    t = fm.store.find("o1")
    assert t.status == QUEUED and "vuelve a la cola" in t.waiting
    # El emisor no llegó a recibir nada: ahora sí, QUEUED.
    assert responses(fm.bus, "o1")[-1]["status"] == QUEUED
    fm.disp.tick()                                       # mir-2 libre → se lanza ya, una sola vez
    assert responses(fm.bus, "o1")[-1]["assignedSerial"] == "mir-2"


def test_sin_red_queda_en_duda_y_se_reintenta(world):
    fm, drivers, clock = world
    drivers["mir-1"] = CrashDriver(receives=True)
    fm.robots["mir-1"].driver = drivers["mir-1"]
    with pytest.raises(Crash):
        send(fm.disp, "o1")
    drivers["mir-1"].fail_find = True
    fm = fm.restart(drivers, clock)
    t = fm.store.find("o1")
    assert t.status == ASSIGNING and "en duda" in t.waiting and "sin red" in t.waiting
    fm.disp.tick()
    q = last_queue(fm.bus)
    assert q[0]["orderId"] == "o1" and q[0]["status"] == ASSIGNING and q[0]["serial"] == "mir-1"
    drivers["mir-1"].fail_find = False                   # vuelve la red
    clock.t += DOUBT_RETRY_S - 1
    fm.disp.tick()
    assert fm.store.find("o1").status == ASSIGNING
    clock.t += 2
    fm.disp.tick()
    assert fm.store.find("o1").status == ASSIGNED and last_queue(fm.bus) == []


def test_una_en_duda_se_descarta_cancelandola(world):
    fm, drivers, clock = world
    drivers["mir-1"] = CrashDriver(receives=True)
    fm.robots["mir-1"].driver = drivers["mir-1"]
    with pytest.raises(Crash):
        send(fm.disp, "o1")
    drivers["mir-1"].fail_find = True
    fm = fm.restart(drivers, clock)
    cancel(fm.disp, "o1")
    r = responses(fm.bus, "o1")[-1]
    assert r["status"] == CANCELLED and "sigue en el robot" in r["description"]


def test_robot_que_no_corre_en_este_fm_no_se_toca(world):
    fm, drivers, clock = world
    send(fm.disp, "o1"); send(fm.disp, "o2")             # o1 → mir-1, o2 → mir-2
    fm = fm.restart(drivers, clock, running=("mir-1",))  # run_fm.py --robot mir-1
    fm.disp.tick()
    assert fm.store.find("o2").status == ASSIGNED and responses(fm.bus, "o2") == []


def requeue(disp, order_id):
    params = [{"key": "orderId", "value": order_id}] if order_id else []
    disp.handle(topic(FLEET_MAN, "fleet", "instantActions"), json.dumps({
        **make_header(HeaderCounter(), FLEET_MAN, "fleet", "instantActions"),
        "actions": [{"actionType": "requeueOrder", "actionId": "r1", "blockingType": "NONE",
                     "actionParameters": params}]}))


def test_una_en_duda_se_relanza_y_vuelve_a_su_sitio(world):
    # Decisión 163: alguien comprueba en el robot que no la tiene y la relanza.
    fm, drivers, clock = world
    send(fm.disp, "o0")                                  # mir-1 ocupado
    drivers["mir-2"] = CrashDriver(receives=False)
    fm.robots["mir-2"].driver = drivers["mir-2"]
    with pytest.raises(Crash):
        send(fm.disp, "o1")                              # a mir-2, y cae el FM
    drivers["mir-2"] = RecoverDriver(fail_find=True)     # al volver, mir-2 no contesta
    fm = fm.restart(drivers, clock)
    send(fm.disp, "o2"); send(fm.disp, "o3")             # o2 → mir-2; o3 espera
    assert fm.store.find("o1").status == ASSIGNING
    old_tag = fm.store.find("o1").tag
    clock.t += 600                                       # diez minutos en duda
    requeue(fm.disp, "o1")
    r = responses(fm.bus, "o1")[-1]
    assert r["status"] == QUEUED and r["queuePosition"] == 1          # delante de o3: llegó antes
    assert "no la tenía" in r["description"]
    assert [t["orderId"] for t in last_queue(fm.bus)] == ["o1", "o3"]
    t1 = fm.store.find("o1")
    assert t1.received_at == clock.t and t1.tag != old_tag   # cuenta desde ahora; otra etiqueta
    finish(fm.robots["mir-2"])
    fm.disp.tick()
    assert responses(fm.bus, "o1")[-1]["assignedSerial"] == "mir-2"
    assert [t["orderId"] for t in last_queue(fm.bus)] == ["o3"]


def test_requeue_solo_para_una_en_duda(world):
    fm, drivers, clock = world
    send(fm.disp, "o1"); send(fm.disp, "o2"); send(fm.disp, "o3")      # o3 en la cola
    for oid in ("o3", "o1", "nada"):
        requeue(fm.disp, oid)
        r = responses(fm.bus)[-1]
        assert r["status"] == "REQUEUE_FAILED" and r["errorType"] == "INVALID_INSTANT_ACTION"
        assert "no está en duda" in r["errorDescription"]
    requeue(fm.disp, None)
    assert "orderId" in responses(fm.bus)[-1]["errorDescription"]
    assert fm.store.find("o1").status == ASSIGNED and fm.store.find("o3").status == QUEUED
