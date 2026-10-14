"""Aplicar en caliente (decisión 91): qué se aplica, qué pide reiniciar y que
la config cambia entera o nada."""
from fm.adapters.sim import SimDriver, SimRobotConfig
from fm.config import AutoChargeConfig, FleetConfig, MqttConfig, RobotConfig
from fm.reload import Reloader, restart_reasons
from fm.robot import Robot


def _cfg(actions1=("coger",), floor=20.0, host="h1", robots=("sim-1",), **over):
    rs = {}
    for s in robots:
        acts = list(actions1) if s == "sim-1" else ["coger"]
        rs[s] = RobotConfig(s, driver="sim", battery_min=25, action_types=frozenset(acts),
                            raw={"driver": "sim", "host": host if s == "sim-1" else "h",
                                 "battery_min": 25, "actions": {a: {} for a in acts}})
    return FleetConfig(robots=rs, auto_charge=AutoChargeConfig(battery_floor=floor),
                       actions={"coger": {}, "dejar": {}}, **over)


def _robots(cfg):
    return {s: Robot(rc, SimDriver(SimRobotConfig(s, set(rc.action_types))), cfg.auto_charge)
            for s, rc in cfg.robots.items()}


def test_lo_ligero_se_aplica_en_caliente():
    old = _cfg()
    assert restart_reasons(old, _cfg(actions1=("coger", "dejar"), floor=15), _robots(old)) == []


def test_lo_estructural_pide_reiniciar():
    old = _cfg()
    robots = _robots(old)
    assert restart_reasons(old, _cfg(host="otro"), robots) == ["robots.sim-1: cambia host"]
    assert restart_reasons(old, _cfg(robots=("sim-1", "sim-2")), robots) == ["robots nuevos: sim-2"]
    assert restart_reasons(old, _cfg(mqtt=MqttConfig(port=1884)), robots) == ["cambia la configuración MQTT"]


class _NoHookDriver:
    manufacturer = "X"
    last_error = None


def test_driver_sin_reconfigure_pide_reiniciar_si_cambian_sus_actions():
    old = _cfg()
    robots = _robots(old)
    robots["sim-1"].driver = _NoHookDriver()
    assert restart_reasons(old, _cfg(floor=15), robots) == []          # umbrales: sí
    assert "no admite cambiar sus actions" in restart_reasons(old, _cfg(actions1=("coger", "dejar")), robots)[0]


class _Target:
    cfg = None


def test_reloader_aplica_entre_ticks():
    old = _cfg()
    robots = _robots(old)
    target, factsheets, notes = _Target(), [], []
    rl = Reloader(old, robots, [target], factsheets.append, notes.append)
    new = _cfg(actions1=("coger", "dejar"), floor=15)
    assert rl.request(new) == []
    assert robots["sim-1"].cfg is old.robots["sim-1"]                  # nada cambia hasta el tick
    rl.apply_pending()
    r = robots["sim-1"]
    assert r.cfg.action_types == {"coger", "dejar"} and r.driver.cfg.action_types == {"coger", "dejar"}
    assert r.charge.cfg.battery_floor == 15 and r.reindex
    assert target.cfg is new and rl.cfg is new
    assert factsheets == [r] and notes == [{"applied": True}]


def test_reloader_no_encola_lo_que_pide_reiniciar():
    old = _cfg()
    rl = Reloader(old, _robots(old), [])
    assert rl.request(_cfg(host="otro")) == ["robots.sim-1: cambia host"]
    assert rl.inbox.empty()


def test_si_un_driver_no_acepta_su_bloque_no_cambia_nada(monkeypatch):
    old = _cfg()
    robots = _robots(old)
    notes = []
    rl = Reloader(old, robots, [], notify=notes.append)
    import fm.reload
    monkeypatch.setattr(fm.reload, "make_driver", lambda rc, cfg: 1 / 0)
    rl.request(_cfg(actions1=("coger", "dejar"), floor=15))
    rl.apply_pending()
    assert robots["sim-1"].cfg is old.robots["sim-1"] and robots["sim-1"].charge.cfg.battery_floor == 20
    assert notes[0]["applied"] is False and rl.cfg is old
