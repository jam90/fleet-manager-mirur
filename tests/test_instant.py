"""instantActions (H5): startPause/stopPause/cancelOrder/stateRequest y su
reflejo en `state.instantActionStates[]` / `errors[]`."""
import json

from fm.adapters.base import Job, Telemetry
from fm.adapters.sim import SimDriver, SimRobotConfig
from fm.config import RobotConfig
from fm.instant import apply_instant_actions
from fm.robot import Robot
from fm.vda5050.header import HeaderCounter, make_header, topic
from fm.vda5050.order import Action, parse_instant_actions
from fm.vda5050.schemas import assert_valid
from fm.vda5050.state_builder import to_vda_state
from tests.test_fleet import FakeBus, _setup
from tests.test_orders import order


class FakeDriver:
    manufacturer = "FAKE"
    last_error = None

    def __init__(self):
        self.calls = []

    def translate(self, action):
        return Job(action, "fake")

    def execute(self, job, priority=0):
        return "j1"

    def job_status(self, job_id):
        return "RUNNING"

    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))

    def pause(self):
        self.calls.append(("pause",))

    def resume(self):
        self.calls.append(("resume",))


def _robot():
    d = FakeDriver()
    r = Robot(RobotConfig("r1", action_types=frozenset({"coger"})), d)
    r.last_telemetry = Telemetry(battery=80)
    return r, d


def _ia(*types):
    return [Action(t, f"ia-{i}") for i, t in enumerate(types)]


def test_pause_resume_state_request():
    r, d = _robot()
    published = []
    apply_instant_actions(r, _ia("startPause", "stopPause", "stateRequest"), published.append)
    assert d.calls == [("pause",), ("resume",)]
    assert len(published) == 2                      # stateRequest + el final
    assert [(s.actionType, s.actionStatus) for s in r.orders.instant_states] == [
        ("startPause", "FINISHED"), ("stopPause", "FINISHED"), ("stateRequest", "FINISHED")]
    assert r.orders.instant_errors == []
    st = to_vda_state(make_header(HeaderCounter(), "FAKE", "r1", "state"), r.last_telemetry, r.overlay())
    assert_valid("state", st.to_dict())
    assert len(st.instantActionStates) == 3


def test_cancel_order_con_y_sin_order():
    r, d = _robot()
    apply_instant_actions(r, _ia("cancelOrder"), lambda _: None)
    assert r.orders.instant_states[0].actionStatus == "FAILED"
    assert r.orders.instant_errors[0].errorType == "NO_ORDER_TO_CANCEL"
    assert d.calls == []
    r.execute(order(), r.translate_order(order()))
    assert r.busy
    apply_instant_actions(r, _ia("cancelOrder"), lambda _: None)
    assert d.calls == [("cancel", "j1")]
    assert r.orders.instant_states[0].actionStatus == "FINISHED" and r.orders.instant_errors == []
    ov = r.overlay()
    assert ov.action_states[0].actionStatus == "FAILED" and not r.busy
    assert ov.errors == []                          # cancelar no es un fallo de ejecución
    # una order nueva vuelve a aceptarse tras cancelar
    r.execute(order(order_id="o2"), r.translate_order(order(order_id="o2")))
    assert r.busy


def test_no_soportadas_y_error_del_driver():
    r, d = _robot()
    d.pause = lambda: (_ for _ in ()).throw(ConnectionError("sin red"))
    apply_instant_actions(r, _ia("factsheetRequest", "volar", "startPause"), lambda _: None)
    assert [s.actionStatus for s in r.orders.instant_states] == ["FAILED"] * 3
    types = [e.errorType for e in r.orders.instant_errors]
    assert types == ["INVALID_INSTANT_ACTION"] * 3
    assert "sin red" in r.orders.instant_errors[2].errorDescription
    assert r.orders.instant_errors[1].errorReferences[0].referenceValue == "ia-1"
    # driver sin pause() → FAILED, no excepción
    class Minimal(FakeDriver):
        pause = None
    r.driver = Minimal()
    apply_instant_actions(r, _ia("startPause"), lambda _: None)
    assert "no soporta" in r.orders.instant_errors[0].errorDescription


def test_parse_y_dispatcher():
    disp, robots, bus, states = _setup({"mir-1": "MiR"})
    msg = {**make_header(HeaderCounter(), "MiR", "mir-1", "instantActions"),
           "actions": [{"actionType": "stateRequest", "actionId": "ia-1", "blockingType": "NONE"}]}
    disp.handle(topic("MiR", "mir-1", "instantActions"), json.dumps(msg))
    assert robots["mir-1"].orders.instant_states[0].actionStatus == "FINISHED"
    assert states == ["mir-1", "mir-1"]
    # mensaje sin 'actions' → error suelto VALIDATION_FAILURE en state
    disp.handle(topic("MiR", "mir-1", "instantActions"), json.dumps({**msg, "actions": "x"}))
    assert robots["mir-1"].orders.instant_errors[0].errorType == "VALIDATION_FAILURE"
    assert parse_instant_actions({"actions": []}) == []


def test_sim_pausa_congela_el_job():
    t = {"now": 0.0}
    d = SimDriver(SimRobotConfig("sim-1", {"coger"}, duration_s=10), clock=lambda: t["now"])
    j = d.execute(d.translate(Action("coger", "a1")))
    t["now"] += 5
    d.pause()
    assert d.poll().paused and not d.poll().driving
    t["now"] += 100
    assert d.job_status(j) == "RUNNING"
    d.resume()
    t["now"] += 4
    assert d.job_status(j) == "RUNNING" and d.poll().driving
    t["now"] += 2
    assert d.job_status(j) == "FINISHED"
