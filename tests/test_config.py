"""Carga de fleet.yaml: parte genérica (core) + parte del driver + registro."""
import pytest

from fm.adapters import make_driver
from fm.adapters.mir import MirDriver
from fm.adapters.mir.config import parse_config
from fm.config import ConfigError, load_config

YAML = """
mqtt: { manufacturer: MiR }
auto_charge: { battery_floor: 20 }
drivers:
  mir:
    mission_group: g
    charge_mission: Carga
robots:
  mir-1:
    host: 10.0.0.1
    battery_min: 30
    actions:
      coger: { mission: coger }
      ir_a: { mission: "Ir", position_inputs: [target_pos] }
  mir-2:
    manufacturer: MiR-Custom
    mission_group: otro
    actions: { coger: { mission: coger } }
"""


def _write(tmp_path, text):
    p = tmp_path / "fleet.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_core_solo_lee_lo_generico(tmp_path):
    cfg = load_config(_write(tmp_path, YAML), env_path=tmp_path / "no.env")
    r1 = cfg.robots["mir-1"]
    assert r1.driver == "mir" and r1.manufacturer is None and r1.battery_min == 30
    assert r1.action_types == {"coger", "ir_a"}
    assert r1.raw["host"] == "10.0.0.1"          # el core no lo interpreta
    assert cfg.driver_defaults("mir") == {"mission_group": "g", "charge_mission": "Carga"}
    assert cfg.auto_charge.battery_floor == 20 and not hasattr(cfg.auto_charge, "mission")


def test_driver_fusiona_defaults_y_entorno(tmp_path):
    cfg = load_config(_write(tmp_path, YAML), env_path=tmp_path / "no.env")
    env = {"MIR_AUTH": "Basic x", "MIR_HOST_MIR_2": "10.0.0.2", "MIR_AUTH_MIR_2": "Basic y"}
    m1 = parse_config("mir-1", cfg.robots["mir-1"].raw, cfg.driver_defaults("mir"), env)
    assert (m1.host, m1.auth, m1.mission_group, m1.charge_mission) == ("10.0.0.1", "Basic x", "g", "Carga")
    assert m1.actions["ir_a"].position_inputs == ["target_pos"]
    m2 = parse_config("mir-2", cfg.robots["mir-2"].raw, cfg.driver_defaults("mir"), env)
    assert (m2.host, m2.auth, m2.mission_group) == ("10.0.0.2", "Basic y", "otro")   # el robot manda
    with pytest.raises(ConfigError, match="host"):
        parse_config("mir-2", cfg.robots["mir-2"].raw, {}, {})
    with pytest.raises(ConfigError, match="mission"):
        parse_config("x", {"host": "h", "actions": {"coger": {}}}, {}, {})


def test_registro_make_driver(tmp_path):
    cfg = load_config(_write(tmp_path, YAML), env_path=tmp_path / "no.env")
    d1 = make_driver(cfg.robots["mir-1"], cfg, env={"MIR_AUTH": "Basic x"})
    assert isinstance(d1, MirDriver) and d1.manufacturer == "MiR"
    d2 = make_driver(cfg.robots["mir-2"], cfg, env={"MIR_AUTH": "Basic x", "MIR_HOST_MIR_2": "h"})
    assert d2.manufacturer == "MiR-Custom"
    cfg.robots["mir-1"].driver = "omron"
    with pytest.raises(ConfigError, match="omron"):
        make_driver(cfg.robots["mir-1"], cfg, env={})


@pytest.mark.parametrize("bad, hint", [
    ("mission_group: g\nrobots: {}", "drivers.mir.mission_group"),
    ("positions_allowlist: [a]\nrobots: {}", "drivers.mir.positions_allowlist"),
    ("auto_charge: { mission: Carga }\nrobots: {}", "drivers.mir.charge_mission"),
])
def test_yaml_antiguo_falla_con_pista(tmp_path, bad, hint):
    with pytest.raises(ConfigError, match=hint):
        load_config(_write(tmp_path, bad), env_path=tmp_path / "no.env")
