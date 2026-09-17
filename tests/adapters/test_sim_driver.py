"""Lo específico del `SimDriver`: tiempo simulado, batería, fallos forzados."""
from fm.adapters import make_driver
from fm.adapters.sim import SimDriver, SimRobotConfig, parse_config
from fm.config import FleetConfig, RobotConfig
from fm.vda5050.order import Action


def _sim(**over):
    t = {"now": 100.0}
    cfg = SimRobotConfig("sim-1", {"coger", "dejar"}, battery=50, duration_s=10,
                         drain_pct_per_s=1.0, fail_actions={"dejar"}, **over)
    return SimDriver(cfg, clock=lambda: t["now"]), t


def test_ejecuta_descarga_y_falla_lo_configurado():
    d, t = _sim(pose=(1.0, 2.0, 0.5))
    d.connect()
    tel = d.poll()
    assert tel.pose == (1.0, 2.0, 0.5) and tel.map_id == "sim-map" and not tel.driving
    j1 = d.execute(d.translate(Action("coger", "a1")))
    t["now"] += 4
    tel = d.poll()
    assert d.job_status(j1) == "RUNNING" and tel.driving and tel.battery == 46.0
    t["now"] += 7
    assert d.job_status(j1) == "FINISHED" and not d.poll().driving
    j2 = d.execute(d.translate(Action("dejar", "a2")))
    t["now"] += 11
    assert d.job_status(j2) == "FAILED"


def test_cancel_y_carga():
    d, t = _sim()
    j = d.execute(d.translate(Action("coger", "a1")))
    d.cancel(j)
    assert d.job_status(j) == "FAILED"
    cj = d.execute(d.charge_job())
    t["now"] += 5
    tel = d.poll()
    assert tel.charging is True and not tel.driving and tel.battery == 55.0
    t["now"] += 30
    assert d.job_status(cj) == "FINISHED"


def test_registro_y_parse_config():
    cfg = FleetConfig(robots={"sim-1": RobotConfig("sim-1", driver="sim", raw={
        "driver": "sim", "battery": 70, "pose": [0, 0, 0], "actions": {"coger": {}}})},
        drivers={"sim": {"duration_s": 2}})
    d = make_driver(cfg.robots["sim-1"], cfg, env={})
    assert isinstance(d, SimDriver) and d.manufacturer == "SIM"
    assert d.cfg.duration_s == 2 and d.cfg.battery == 70 and d.cfg.action_types == {"coger"}
    assert parse_config("s", {}, {}).pose is None
