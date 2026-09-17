"""`MirDriver` contra un cliente falso: connect → poll → translate → execute → job_status."""
import pytest

from fm.adapters.mir import MirDriver
from fm.adapters.mir.client import MirClient
from fm.config import ActionConfig, RobotConfig
from fm.vda5050.order import Action, ActionParameter, OrderRejected

CFG = RobotConfig("mir-1", "h", "a", 25, {
    "coger": ActionConfig("coger", "coger"),
    "ir_a": ActionConfig("ir_a", "Ir a posición", position_inputs=["target_pos"]),
})
STATUS = {"state_id": 3, "state_text": "Ready", "battery_percentage": 70.0, "map_id": "m",
          "position": {"x": 0, "y": 0, "orientation": 0}, "mission_queue_id": None}


class _FakeClient(MirClient):
    def __init__(self, fail_status=False):
        super().__init__("fake", "Basic x")
        self.fail_status = fail_status
        self.status = dict(STATUS)
        self.queue: dict[int, str] = {}
        self.posted: list[dict] = []
        self.deleted: list[int] = []

    def _req(self, method, path, json=None):
        if method == "GET" and path == "status":
            if self.fail_status:
                raise ConnectionError("sin red")
            return self.status
        if path == "mission_groups":
            return [{"name": "g", "guid": "g-guid"}]
        if path == "mission_groups/g-guid/missions":
            return [{"name": "coger", "guid": "m-coger"}, {"name": "Ir a posición", "guid": "m-ir"},
                    {"name": "Carga", "guid": "m-carga"}]
        if path == "positions":
            return [{"name": "P1", "guid": "p-1", "map": "/v2.0.0/maps/m"}]
        if path.startswith("missions/") and path.endswith("/actions"):
            return [{"parameters": [{"input_name": "target_pos"}]}] if "m-ir" in path else []
        if method == "POST" and path == "mission_queue":
            self.posted.append(json)
            qid = 100 + len(self.posted)
            self.queue[qid] = "Pending"
            return {"id": qid}
        if method == "GET" and path.startswith("mission_queue/"):
            return {"state": self.queue[int(path.split("/")[1])]}
        if method == "DELETE" and path.startswith("mission_queue/"):
            self.deleted.append(int(path.split("/")[1]))
            return None
        raise AssertionError(f"llamada inesperada {method} {path}")


def _driver(**kw):
    c = _FakeClient(**kw)
    return MirDriver(CFG, c, mission_group="g", charge_mission="Carga"), c


def test_connect_indices_y_translate():
    d, _ = _driver()
    assert d.manufacturer == "MiR"
    assert d.connect() and d.indexed
    assert d.missions == {"coger": "m-coger", "Ir a posición": "m-ir", "Carga": "m-carga"}
    assert d.positions == {"P1": "p-1"}
    job = d.translate(Action("ir_a", "a1", actionParameters=[ActionParameter("target_pos", "P1")]))
    assert job.label == "mission 'Ir a posición'"
    assert job.payload.parameters == [{"id": "target_pos", "value": "p-1"}]
    with pytest.raises(OrderRejected) as e:
        d.translate(Action("volar", "a2"))
    assert e.value.error_type == "INVALID_ORDER_ACTION"


def test_sin_indices_rechaza_y_poll_tolerante():
    d, c = _driver(fail_status=True)
    assert not d.connect() and not d.indexed
    assert d.poll() is None and "ConnectionError" in d.last_error
    with pytest.raises(OrderRejected) as e:
        d.translate(Action("coger", "a1"))
    assert e.value.error_type == "MOBILE_ROBOT_NOT_AVAILABLE"
    c.fail_status = False
    t = d.poll()
    assert t is not None and t.battery == 70.0 and d.last_error is None


def test_execute_job_status_y_propia_vs_ajena():
    d, c = _driver()
    d.connect()
    job = d.translate(Action("coger", "a1"))
    jid = d.execute(job, priority=5)
    assert jid == "101" and c.posted == [{"mission_id": "m-coger", "priority": 5}]
    assert d.job_status(jid) == "WAITING"
    # El robot ejecuta NUESTRA mission → no es ajena
    c.queue[101] = "Executing"
    c.status.update(state_id=5, mission_queue_id=101)
    assert d.job_status(jid) == "RUNNING"
    assert not d.poll().foreign_busy
    # Termina; luego alguien lanza otra desde la web → ajena
    c.queue[101] = "Done"
    assert d.job_status(jid) == "FINISHED"
    c.status.update(mission_queue_id=555)
    assert d.poll().foreign_busy
    d.cancel(jid)
    assert c.deleted == [101]


def test_job_status_none_si_falla_y_estado_raro():
    d, c = _driver()
    assert d.job_status("999") is None          # KeyError en el fake = fallo de red
    c.queue[7] = "Marciano"
    assert d.job_status("7") is None


def test_charge_job():
    d, _ = _driver()
    assert d.charge_job() is None               # sin índices aún
    d.connect()
    job = d.charge_job()
    assert job.payload.mission_guid == "m-carga" and job.action.actionType == "charge"
    d2 = MirDriver(CFG, _FakeClient(), mission_group="g")
    d2.connect()
    assert d2.charge_job() is None              # sin mission de carga configurada
