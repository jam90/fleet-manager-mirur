"""Auto-carga (H4): `ChargeGuard` con driver falso y end-to-end con el sim."""
from fm.adapters.base import Job, Telemetry
from fm.adapters.sim import SimDriver, SimRobotConfig
from fm.assigner import assign
from fm.charge import ChargeGuard
from fm.config import AutoChargeConfig, FleetConfig, RobotConfig
from fm.robot import Robot
from fm.vda5050.header import HeaderCounter, make_header
from fm.vda5050.order import Action
from fm.vda5050.schemas import assert_valid
from fm.vda5050.state_builder import to_vda_state

CFG = AutoChargeConfig(battery_floor=20, priority=10, abort_cooldown_s=60)


class FakeDriver:
    manufacturer = "FAKE"
    last_error = None

    def __init__(self, supports=True):
        self.supports = supports
        self.executed: list[tuple[Job, int]] = []
        self.status = "WAITING"
        self.fail_execute = False

    def charge_job(self):
        return Job(Action("charge", "auto"), "fake 'charge'") if self.supports else None

    def execute(self, job, priority=0):
        if self.fail_execute:
            raise ConnectionError("sin red")
        self.executed.append((job, priority))
        return str(len(self.executed))

    def job_status(self, job_id):
        return self.status


def _guard(driver=None, now=None):
    t = {"now": 1000.0}
    g = ChargeGuard("r1", CFG, clock=lambda: t["now"])
    return g, driver or FakeDriver(), t


def test_postea_bajo_el_suelo_y_sigue_el_job():
    g, d, t = _guard()
    g.tick(d, Telemetry(battery=25))
    assert not g.active and d.executed == []
    g.tick(d, Telemetry(battery=19.9))
    assert g.active and d.executed[0][1] == 10 and g.job_id == "1"
    # activa: no vuelve a postear aunque siga baja; refleja RUNNING → charging
    d.status = "RUNNING"
    g.tick(d, Telemetry(battery=15))
    assert g.charging and len(d.executed) == 1
    ov = g.decorate(__import__("fm.vda5050.state_builder", fromlist=["StateOverlay"]).StateOverlay())
    assert ov.charging is True and ov.information[0].infoType == "AUTO_CHARGE"
    d.status = "FINISHED"
    g.tick(d, Telemetry(battery=70))
    assert not g.active and not g.charging
    g.tick(d, Telemetry(battery=70))
    assert len(d.executed) == 1


def test_no_postea_sin_telemetria_ni_no_disponible():
    g, d, t = _guard()
    g.tick(d, None)
    g.tick(d, Telemetry(battery=5, available=False))
    assert d.executed == []


def test_cooldown_tras_failed_y_tras_error_de_red():
    g, d, t = _guard()
    g.tick(d, Telemetry(battery=10))
    d.status = "FAILED"
    g.tick(d, Telemetry(battery=10))
    assert not g.active
    g.tick(d, Telemetry(battery=10))
    assert len(d.executed) == 1            # en cooldown
    t["now"] += 61
    d.status = "WAITING"
    g.tick(d, Telemetry(battery=10))
    assert len(d.executed) == 2
    # error al postear → también cooldown
    d.status = "FINISHED"; g.tick(d, Telemetry(battery=10))
    d.fail_execute = True
    g.tick(d, Telemetry(battery=10))
    d.fail_execute = False
    g.tick(d, Telemetry(battery=10))
    assert len(d.executed) == 2
    t["now"] += 61
    g.tick(d, Telemetry(battery=10))
    assert len(d.executed) == 3


def test_driver_sin_carga_desactiva():
    g, d, t = _guard(FakeDriver(supports=False))
    g.tick(d, Telemetry(battery=5))
    g.tick(d, Telemetry(battery=5))
    assert g.unsupported and d.executed == []


def test_end_to_end_con_sim_busy_y_state():
    """Batería cae → carga (busy para el asignador, state válido con
    charging=True) → termina → vuelve a ser asignable."""
    t = {"now": 0.0}
    d = SimDriver(SimRobotConfig("sim-1", {"coger"}, battery=21, duration_s=10,
                                 drain_pct_per_s=1.0, charge_duration_s=30), clock=lambda: t["now"])
    fleet = FleetConfig(robots={"sim-1": RobotConfig("sim-1", battery_min=25, action_types=frozenset({"coger"}))},
                        auto_charge=CFG)
    r = Robot(fleet.robots["sim-1"], d, CFG)
    r.connect(); r.poll()
    r.execute(__import__("tests.test_orders", fromlist=["order"]).order(), r.driver.translate(Action("coger", "a1")))
    t["now"] += 5; r.poll()                # 21 - 5 = 16 % → carga se encola detrás de la order
    assert r.charge.active and r.charge.status == "WAITING" and r.orders.busy
    t["now"] += 6; r.poll()                # order terminada, carga RUNNING
    assert r.orders.overlay().action_states[0].actionStatus == "FINISHED"
    assert r.charge.charging and r.busy
    st = to_vda_state(make_header(HeaderCounter(), "SIM", "sim-1", "state"), r.last_telemetry, r.overlay())
    assert_valid("state", st.to_dict())
    assert st.powerSupply.charging is True
    assert [i.infoType for i in st.information] == ["AUTO_CHARGE"]
    res = assign("coger", {"sim-1": r.snapshot()}, fleet)
    assert not res.ok and res.rejections["sim-1"].startswith("batería")   # 10 % < battery_min
    t["now"] += 31; r.poll()               # carga terminada, batería recuperada
    assert not r.charge.active and not r.busy and r.last_telemetry.battery > 25
    assert assign("coger", {"sim-1": r.snapshot()}, fleet).ok
