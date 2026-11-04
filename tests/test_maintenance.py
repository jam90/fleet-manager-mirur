"""Modo mantenimiento por robot (decisiones 167–170): sale del reparto, no
recibe orders dirigidas, ni carga ni vuelta a la espera; si tiene trabajo
del FM lo termina antes (pendiente) y luego publica `operatingMode` SERVICE;
se guarda en SQLite y vuelve al arrancar."""
import json

from fm.adapters.sim import SimDriver, SimRobotConfig
from fm.assigner import assign
from fm.config import AutoChargeConfig, FleetConfig, IdleReturnConfig, RobotConfig
from fm.instant import apply_instant_actions
from fm.maintenance import MemoryMaintenanceStore, SqliteMaintenanceStore, attach
from fm.robot import Robot
from fm.vda5050.factsheet import INSTANT_ACTIONS
from fm.vda5050.header import HeaderCounter, make_header, topic
from fm.vda5050.order import Action, OrderRejected
from fm.vda5050.schemas import assert_valid
from tests.test_fleet import FLEET_MAN, _order, _setup, last_response
from tests.test_orders import order

ESPERA = IdleReturnConfig("ir_a_espera", 30.0, {"target_pos": "Espera"})
CARGA = AutoChargeConfig(battery_floor=20, priority=10, abort_cooldown_s=60)


def _robot(battery=90, drain=0.0, idle=None):
    t = {"now": 0.0}
    d = SimDriver(SimRobotConfig("sim-1", {"coger", "ir_a_espera"}, battery=battery, duration_s=10,
                                 drain_pct_per_s=drain, charge_duration_s=30), clock=lambda: t["now"])
    rc = RobotConfig("sim-1", battery_min=25, action_types=frozenset({"coger", "ir_a_espera"}),
                     **({"idle_return": idle} if idle else {}))
    r = Robot(rc, d, CARGA, clock=lambda: t["now"])
    r.connect()
    r.poll()
    return r, d, t


def _run(r, t, seconds):
    for _ in range(int(seconds)):
        t["now"] += 1
        r.poll()


def _state(r):
    st = r.state(make_header(HeaderCounter(), "SIM", "sim-1", "state"))
    assert_valid("state", st.to_dict())
    return st


def _infos(st):
    return {i.infoType: i.infoDescriptor for i in st.information}


def test_libre_entra_en_el_acto_y_sale_del_reparto():
    r, d, t = _robot()
    fleet = FleetConfig(robots={"sim-1": r.cfg})
    assert r.start_maintenance(now=1_800_000_000.0)
    assert r.in_maintenance and r.in_service and not r.available
    st = _state(r)
    assert st.operatingMode == "SERVICE"
    assert _infos(st)["MAINTENANCE"] == "2027-01-15T08:00:00.000Z"
    res = assign("coger", {"sim-1": r.snapshot()}, fleet)
    assert not res.ok and "mantenimiento" in res.rejections["sim-1"]
    # Pedirlo otra vez no cambia la hora.
    assert not r.start_maintenance(now=1_900_000_000.0)
    assert r.maintenance_since == 1_800_000_000.0
    assert r.stop_maintenance() and not r.stop_maintenance()
    assert r.available and _state(r).operatingMode == "AUTOMATIC"
    assert assign("coger", {"sim-1": r.snapshot()}, fleet).ok


def test_una_order_dirigida_se_rechaza_con_el_motivo():
    r, d, t = _robot()
    r.start_maintenance()
    try:
        r.translate_order(order())
        raise AssertionError("debía rechazarse")
    except OrderRejected as e:
        assert e.error_type == "MOBILE_ROBOT_NOT_AVAILABLE" and "mantenimiento" in e.description


def test_con_una_order_en_curso_la_termina_y_luego_entra():
    r, d, t = _robot()
    r.execute(order(), d.translate(Action("coger", "a1")))
    _run(r, t, 2)
    r.start_maintenance()
    assert r.in_maintenance and not r.in_service and not r.available
    st = _state(r)
    assert st.operatingMode == "AUTOMATIC"                       # sigue con su order: no se aborta
    assert "al terminar su order" in _infos(st)["MAINTENANCE_PENDING"]
    assert [a.actionStatus for a in st.actionStates] == ["RUNNING"]
    _run(r, t, 10)                                               # la order acaba
    st = _state(r)
    assert [a.actionStatus for a in st.actionStates] == ["FINISHED"]
    assert r.in_service and st.operatingMode == "SERVICE" and "MAINTENANCE" in _infos(st)


def test_no_se_lanza_carga_pero_la_que_estaba_se_termina():
    # Batería bajo el suelo: en mantenimiento, ninguna carga.
    r, d, t = _robot(battery=15)
    assert r.charge.active                                       # el primer poll ya la lanzó
    r.charge.job_id = None                                       # (se olvida para probar desde cero)
    r.start_maintenance()
    _run(r, t, 5)
    assert not r.charge.active and r.in_service
    # Carga ya en marcha al pedirlo: pendiente hasta que acaba, y no se repite.
    r, d, t = _robot(battery=15)
    assert r.charge.active
    r.start_maintenance()
    assert not r.in_service and "su carga" in r.maintenance_reason
    _run(r, t, 31)
    assert not r.charge.active and r.in_service
    n = len(d._jobs)
    r.last_telemetry.battery = 10                                # aunque vuelva a bajar
    _run(r, t, 5)
    assert len(d._jobs) == n


def test_corta_la_vuelta_a_la_espera_y_no_la_vuelve_a_lanzar():
    r, d, t = _robot(idle=ESPERA)
    _run(r, t, 32)
    assert r.idle.active
    r.start_maintenance()
    assert not r.idle.active                                     # cancelada al entrar
    _run(r, t, 60)
    assert not r.idle.active and r.idle.remaining() is None      # ni cuenta
    r.stop_maintenance()
    _run(r, t, 32)                                               # al salir, vuelve a contar
    assert r.idle.active


def test_instant_actions_y_se_guarda():
    r, d, t = _robot()
    store = MemoryMaintenanceStore()
    r.maintenance_store = store
    apply_instant_actions(r, [Action("startMaintenance", "ia-1"), Action("startMaintenance", "ia-2")],
                          lambda _: None)
    assert [s.actionStatus for s in r.orders.instant_states] == ["FINISHED", "FINISHED"]   # idempotente
    assert set(store.load()) == {"sim-1"}
    apply_instant_actions(r, [Action("stopMaintenance", "ia-3")], lambda _: None)
    assert r.orders.instant_states[0].actionStatus == "FINISHED" and store.load() == {}
    assert {"startMaintenance", "stopMaintenance"} <= set(INSTANT_ACTIONS)


def test_sqlite_sobrevive_al_reinicio(tmp_path):
    db = tmp_path / "fleet.sqlite"
    store = SqliteMaintenanceStore(db)
    store.set("mir-1", 1_800_000_000.0)
    store.set("mir-2", 1_800_000_100.0)
    store.set("mir-2", None)
    store.close()
    r, d, t = _robot()
    r.serial = "mir-1"
    store = SqliteMaintenanceStore(db)                           # "arranca" el FM
    attach({"mir-1": r}, store)
    assert r.in_maintenance and r.maintenance_since == 1_800_000_000.0
    r.stop_maintenance()
    assert SqliteMaintenanceStore(db).load() == {}


def test_fleet_order_espera_en_la_cola_hasta_que_sale():
    disp, robots, bus, states = _setup({"mir-1": "MiR"})
    robots["mir-1"].start_maintenance()
    disp.handle(topic(FLEET_MAN, "fleet", "order"), _order(FLEET_MAN, "fleet"))
    resp = last_response(bus)
    assert resp["status"] == "QUEUED" and "mantenimiento" in resp["description"]
    # Por el topic del robot, como lo manda la web.
    msg = {**make_header(HeaderCounter(), "MiR", "mir-1", "instantActions"),
           "actions": [{"actionType": "stopMaintenance", "actionId": "ia-1", "blockingType": "NONE"}]}
    disp.handle(topic("MiR", "mir-1", "instantActions"), json.dumps(msg))
    assert not robots["mir-1"].in_maintenance
    disp.tick()
    assert last_response(bus)["status"] == "ASSIGNED"
