"""Tests de contrato: lo que el core espera de CUALQUIER driver, parametrizado
con cada implementación. Si un driver nuevo no pasa esto, el core no funcionará
con él. Escenario: connect → poll → translate → execute → job_status → cancel.

Cada fixture devuelve `(driver, avanzar)`: `avanzar()` hace que el job en
curso termine (tiempo simulado en `sim`, estado de cola en `mir`).
"""
import pytest

from fm.adapters.base import Job
from fm.adapters.mir import MirDriver
from fm.adapters.mir.config import ActionConfig, MirRobotConfig
from fm.adapters.sim import SimDriver, SimRobotConfig
from fm.vda5050.order import Action, OrderRejected
from tests.adapters.test_mir_driver import _FakeClient


def _sim():
    t = {"now": 0.0}
    d = SimDriver(SimRobotConfig("sim-1", {"coger"}, battery=80, duration_s=10), clock=lambda: t["now"])

    def avanzar():
        t["now"] += 11
    return d, avanzar


def _mir():
    c = _FakeClient()
    d = MirDriver(MirRobotConfig("mir-1", "h", "a", {"coger": ActionConfig("coger", "coger")},
                                 mission_group="g"), c)

    def avanzar():
        for qid in c.queue:
            c.queue[qid] = "Done"
    return d, avanzar


@pytest.fixture(params=[_sim, _mir], ids=["sim", "mir"])
def driver(request):
    return request.param()


def test_ciclo_completo(driver):
    d, avanzar = driver
    assert isinstance(d.manufacturer, str) and d.manufacturer
    assert d.connect() is True
    t = d.poll()
    assert t is not None and 0 <= t.battery <= 100 and t.available and d.last_error is None

    job = d.translate(Action("coger", "a1"))
    assert isinstance(job, Job) and job.action.actionId == "a1" and job.label
    with pytest.raises(OrderRejected) as e:
        d.translate(Action("inexistente", "a2"))
    assert e.value.error_type == "INVALID_ORDER_ACTION"

    job_id = d.execute(job)
    assert isinstance(job_id, str) and job_id
    assert d.job_status(job_id) in ("WAITING", "RUNNING")
    avanzar()
    assert d.job_status(job_id) == "FINISHED"
    assert d.job_status("no-existe") is None


def test_cancel_y_charge_job(driver):
    d, _ = driver
    d.connect()
    job_id = d.execute(d.translate(Action("coger", "a1")))
    d.cancel(job_id)                      # no debe lanzar
    cj = d.charge_job()
    assert cj is None or (isinstance(cj, Job) and cj.action.actionType == "charge")
