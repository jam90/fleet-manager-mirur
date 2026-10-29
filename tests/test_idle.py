"""Vuelta a la espera (decisión 136): `IdleGuard` con el driver sim, solo y
dentro de `Robot`, y su bloque `idle_return` de fleet.yaml."""
import pytest

from fm.adapters.sim import SimDriver, SimRobotConfig
from fm.assigner import assign
from fm.config import ConfigError, FleetConfig, IdleReturnConfig, RobotConfig, load_config
from fm.idle import IdleGuard
from fm.reload import restart_reasons
from fm.robot import Robot
from fm.vda5050.order import Action
from tests.test_orders import order

ESPERA = IdleReturnConfig("ir_a_espera", 30.0, {"target_pos": "Espera"})


def _robot(idle=ESPERA, t=None):
    t = t if t is not None else {"now": 0.0}
    d = SimDriver(SimRobotConfig("sim-1", {"coger", "ir_a_espera"}, battery=90, duration_s=10,
                                 drain_pct_per_s=0.0), clock=lambda: t["now"])
    rc = RobotConfig("sim-1", battery_min=25, action_types=frozenset({"coger", "ir_a_espera"}), idle_return=idle)
    r = Robot(rc, d, clock=lambda: t["now"])
    r.connect()
    return r, d, t


def _run(r, t, seconds):
    for _ in range(int(seconds)):
        t["now"] += 1
        r.poll()


def _parks(d):
    return [j for j in d._jobs.values() if j.job.action.actionType == "ir_a_espera"]


def test_tras_la_order_espera_30s_y_va_una_sola_vez():
    r, d, t = _robot()
    r.execute(order(), d.translate(Action("coger", "a1")))
    _run(r, t, 10)                                   # la order termina: empieza a contar
    assert not r.orders.busy and _parks(d) == [] and r.idle.remaining() == pytest.approx(30)
    _run(r, t, 29)
    assert _parks(d) == [] and r.idle.remaining() == pytest.approx(1)
    # El contador va en el state, para la web.
    assert [(i.infoType, i.infoDescriptor) for i in r.overlay().information] == [("IDLE_RETURN_IN", "1")]
    _run(r, t, 2)                                    # 30 s parado: a la espera
    assert len(_parks(d)) == 1 and r.idle.active
    park = _parks(d)[0]
    assert park.job.action.params() == {"target_pos": "Espera"}
    assert [i.infoType for i in r.overlay().information] == ["IDLE_RETURN"]
    _run(r, t, 120)                                  # llega y se queda: no se repite
    assert len(_parks(d)) == 1 and not r.idle.active and r.idle.remaining() is None
    r.execute(order(order_id="o2"), d.translate(Action("coger", "a2")))
    _run(r, t, 11 + 31)                              # otra order: vuelve a armarse
    assert len(_parks(d)) == 2


def test_al_arrancar_el_fm_un_robot_parado_se_va_a_la_espera():
    r, d, t = _robot()
    _run(r, t, 32)
    assert len(_parks(d)) == 1


def test_una_order_durante_la_espera_la_cancela_y_no_la_rechaza():
    r, d, t = _robot()
    _run(r, t, 32)
    assert r.idle.active and not r.busy             # ir a la espera no ocupa al robot
    fleet = FleetConfig(robots={"sim-1": r.cfg})
    assert assign("coger", {"sim-1": r.snapshot()}, fleet).ok
    job_id = r.execute(order(), d.translate(Action("coger", "a1")))
    park = _parks(d)[0]
    assert park.result == "cancelado" and not r.idle.active
    _run(r, t, 1)
    assert d.job_status(job_id) == "RUNNING"         # la order no espera detrás de la espera


def test_cuenta_solo_parado_de_verdad():
    """Pausa, manual, fuera del site o una mission de la web reinician la cuenta."""
    t = {"now": 0.0}
    g = IdleGuard("r1", ESPERA, clock=lambda: t["now"])
    d = SimDriver(SimRobotConfig("r1", {"ir_a_espera"}), clock=lambda: t["now"])
    for ready in (True, False):                       # 20 s listo, 1 s no (pausa, manual…)
        for _ in range(20 if ready else 1):
            t["now"] += 1
            g.tick(d, busy=False, ready=ready)
    assert g.idle_since is None and not g.active
    for _ in range(29):
        t["now"] += 1
        g.tick(d, busy=False, ready=True)
    assert not g.active
    g.tick(d, busy=True, ready=True)                  # p. ej. una mission lanzada desde la web
    for _ in range(29):
        t["now"] += 1
        g.tick(d, busy=False, ready=True)
    assert not g.active                               # vuelve a contar desde cero
    t["now"] += 2
    g.tick(d, busy=False, ready=True)
    assert g.active


def test_desactivada_o_que_falla_no_insiste():
    r, d, t = _robot(IdleReturnConfig())
    _run(r, t, 100)
    assert _parks(d) == []
    r, d, t = _robot(IdleReturnConfig("no_existe", 5.0))     # el driver la rechaza
    _run(r, t, 60)
    assert d._jobs == {} and not r.idle.armed


# ------------------------------------------------------------- fleet.yaml
YAML = """
actions: {{ coger: {{}}, ir_a_espera: {{}}, ir_a: {{}} }}
{fleet}
robots:
  sim-1: {{ driver: sim, actions: [coger, ir_a_espera, ir_a]{r1} }}
  sim-2: {{ driver: sim, actions: [coger]{r2} }}
"""


def _load(tmp_path, fleet="", r1="", r2=""):
    p = tmp_path / "fleet.yaml"
    p.write_text(YAML.format(fleet=fleet, r1=r1, r2=r2), encoding="utf-8")
    return load_config(p, env_path=tmp_path / "no.env")


def test_config_de_la_flota_y_por_robot(tmp_path):
    cfg = _load(tmp_path)
    assert not cfg.idle_return.enabled and not cfg.robots["sim-1"].idle_return.enabled
    fleet = "idle_return: { action: ir_a_espera, after_s: 45 }"
    cfg = _load(tmp_path, fleet)
    assert cfg.robots["sim-1"].idle_return == IdleReturnConfig("ir_a_espera", 45.0, {})
    assert not cfg.robots["sim-2"].idle_return.enabled          # no tiene la action: no vuelve
    cfg = _load(tmp_path, fleet, r1=", idle_return: { action: ir_a, params: { target_pos: E1 } }")
    assert cfg.robots["sim-1"].idle_return == IdleReturnConfig("ir_a", 45.0, {"target_pos": "E1"})
    cfg = _load(tmp_path, fleet, r1=", idle_return: { after_s: 10 }")
    assert cfg.robots["sim-1"].idle_return == IdleReturnConfig("ir_a_espera", 10.0, {})
    assert not _load(tmp_path, fleet, r1=", idle_return: false").robots["sim-1"].idle_return.enabled


def test_config_errores(tmp_path):
    with pytest.raises(ConfigError, match="no está habilitada en sim-2"):
        _load(tmp_path, r2=", idle_return: { action: ir_a_espera }")
    with pytest.raises(ConfigError, match="after_s"):
        _load(tmp_path, "idle_return: { action: ir_a_espera, after_s: 0 }")
    with pytest.raises(ConfigError, match="claves desconocidas"):
        _load(tmp_path, "idle_return: { action: ir_a_espera, mission: x }")


def test_cambiarla_se_aplica_en_caliente(tmp_path):
    old = _load(tmp_path)
    new = _load(tmp_path, "idle_return: { action: ir_a_espera }", r1=", idle_return: { after_s: 10 }")
    assert restart_reasons(old, new, {}) == []
