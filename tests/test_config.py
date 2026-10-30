"""Carga de fleet.yaml: parte genérica (core) + parte del driver + registro."""
from pathlib import Path

import pytest

from fm.adapters import make_driver
from fm.adapters.mir import MirDriver
from fm.adapters.mir.config import parse_config
from fm.config import ConfigError, load_config

YAML = """
mqtt: { fleet_manufacturer: imperial_fleet }
auto_charge: { battery_floor: 20 }
drivers:
  mir:
    mission_group: g
    charge_mission: Carga
actions:
  coger: { mission: coger }
  ir_a: { mission: "Ir", position_inputs: [target_pos] }
  dejar: { mission: dejar }
robots:
  mir-1:
    host: 10.0.0.1
    battery_min: 30
    actions: [coger, ir_a]
  mir-2:
    manufacturer: MiR-Custom
    mission_group: otro
    actions: [coger]
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
    # el driver recibe las definiciones del catálogo, solo las habilitadas
    assert r1.raw["actions"] == {"coger": {"mission": "coger"},
                                 "ir_a": {"mission": "Ir", "position_inputs": ["target_pos"]}}
    assert cfg.robots["mir-2"].action_types == {"coger"}
    assert sorted(cfg.actions) == ["coger", "dejar", "ir_a"]   # `dejar` sin robot: vale
    assert cfg.driver_defaults("mir") == {"mission_group": "g", "charge_mission": "Carga"}
    assert cfg.auto_charge.battery_floor == 20 and not hasattr(cfg.auto_charge, "mission")
    assert cfg.mqtt.fleet_manufacturer == "imperial_fleet"


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
    ("mqtt: { manufacturer: MiR }\nrobots: {}", "fleet_manufacturer"),
])
def test_yaml_antiguo_falla_con_pista(tmp_path, bad, hint):
    with pytest.raises(ConfigError, match=hint):
        load_config(_write(tmp_path, bad), env_path=tmp_path / "no.env")


def test_number_inputs_y_allowlist_por_action():
    m = parse_config("mir-1", {"host": "h", "actions": {
        "a": {"mission": "M", "number_inputs": ["n"]},
        "b": {"mission": "M", "position_inputs": ["target_pos"], "positions_allowlist": ["P1", "P2"],
              "number_inputs": {"n": {"min": 1, "max": 10, "step": 1}, "m": None}},
    }}, {}, {})
    a, b = m.actions["a"], m.actions["b"]
    assert a.number_inputs == ["n"] and a.number_ranges == {} and a.positions_allowlist is None
    assert b.number_inputs == ["n", "m"] and b.positions_allowlist == {"P1", "P2"}
    assert (b.number_ranges["n"].min, b.number_ranges["n"].max, b.number_ranges["n"].step) == (1, 10, 1)
    assert b.number_ranges["m"].min is None
    for bad, hint in (({"min": 5, "max": 1}, "min > max"), ({"min": "x"}, "deben ser números"),
                      ({"step": 0}, "step")):
        with pytest.raises(ConfigError, match=hint):
            parse_config("mir-1", {"host": "h", "actions": {"a": {"mission": "M", "number_inputs": {"n": bad}}}}, {}, {})


# ---------------------------------------------- catálogo de actions (decisión 92)
def _robots(actions_yaml, catalog="actions: { coger: { mission: coger } }"):
    return f"{catalog}\nrobots:\n  mir-1:\n    host: h\n    actions: {actions_yaml}\n"


def test_formato_antiguo_da_error_con_pista(tmp_path):
    with pytest.raises(ConfigError, match=r"robots.mir-1.actions ya no lleva definiciones.*\[coger\]"):
        load_config(_write(tmp_path, _robots("{ coger: { mission: coger } }")), env_path=tmp_path / "no.env")


@pytest.mark.parametrize("actions, msg", [
    ("[coger, volar]", r"\['volar'\] no están en el catálogo"),
    ("[coger, coger]", r"repetidas \['coger'\]"),
    ("coger", "debe ser una lista"),
])
def test_lista_de_actions_invalida(tmp_path, actions, msg):
    with pytest.raises(ConfigError, match=msg):
        load_config(_write(tmp_path, _robots(actions)), env_path=tmp_path / "no.env")


def test_catalogo_invalido(tmp_path):
    with pytest.raises(ConfigError, match="mapping"):
        load_config(_write(tmp_path, _robots("[]", "actions: [coger]")), env_path=tmp_path / "no.env")
    with pytest.raises(ConfigError, match="actions.coger"):
        load_config(_write(tmp_path, _robots("[]", "actions: { coger: 3 }")), env_path=tmp_path / "no.env")


def test_entrada_vacia_y_robot_sin_actions(tmp_path):
    cfg = load_config(_write(tmp_path, _robots("[coger]", "actions: { coger: }")), env_path=tmp_path / "no.env")
    assert cfg.robots["mir-1"].raw["actions"] == {"coger": {}}
    cfg = load_config(_write(tmp_path, "robots: { mir-1: { host: h } }"), env_path=tmp_path / "no.env")
    assert cfg.robots["mir-1"].action_types == frozenset() and cfg.robots["mir-1"].raw["actions"] == {}


def test_cada_robot_recibe_su_copia(tmp_path):
    # Un driver que tocara su definición no debe cambiar la de otro robot.
    text = YAML.replace("actions: [coger]", "actions: [coger, ir_a]")
    cfg = load_config(_write(tmp_path, text), env_path=tmp_path / "no.env")
    cfg.robots["mir-1"].raw["actions"]["ir_a"]["position_inputs"].append("x")
    assert cfg.robots["mir-2"].raw["actions"]["ir_a"]["position_inputs"] == ["target_pos"]
    assert cfg.actions["ir_a"]["position_inputs"] == ["target_pos"]


def test_ficheros_del_repo_cargan(tmp_path):
    # Los ejemplos del repo (el fleet.yaml real es de cada instalación, fuera
    # de git): el driver de cada robot los acepta.
    env = {"MIR_AUTH": "Basic x"}
    root = Path(__file__).resolve().parent.parent
    for name in ("fleet.example.yaml", "fleet-sim.yaml"):
        cfg = load_config(root / "config" / name, env_path=tmp_path / "no.env")
        for r in cfg.robots.values():
            if r.driver == "mir":
                assert set(parse_config(r.serial, r.raw, cfg.driver_defaults("mir"), env).actions) == r.action_types


# ------------------------------------------- expected_s por action (decisión 123)
def test_expected_s_por_robot_solo_de_sus_actions(tmp_path):
    catalog = "actions: { coger: { mission: coger, expected_s: 90 }, dejar: { mission: dejar } }"
    cfg = load_config(_write(tmp_path, _robots("[coger, dejar]", catalog)), env_path=tmp_path / "no.env")
    assert cfg.robots["mir-1"].expected_s == {"coger": 90.0}
    cfg = load_config(_write(tmp_path, _robots("[dejar]", catalog)), env_path=tmp_path / "no.env")
    assert cfg.robots["mir-1"].expected_s == {}


@pytest.mark.parametrize("bad", ["0", "-5", "rapido", "true"])
def test_expected_s_invalido(tmp_path, bad):
    catalog = f"actions: {{ coger: {{ mission: coger, expected_s: {bad} }} }}"
    with pytest.raises(ConfigError, match="actions.coger.expected_s"):
        load_config(_write(tmp_path, _robots("[coger]", catalog)), env_path=tmp_path / "no.env")


# ------------------------------------- datos de la instalación (decisión 135)
def test_install_paths_de_env_o_del_repo(monkeypatch, tmp_path):
    from fm.config import ROOT, install_paths
    monkeypatch.delenv("FM_CONFIG", raising=False)
    monkeypatch.delenv("FM_CATALOG", raising=False)
    assert install_paths() == (ROOT / "config" / "fleet.yaml", ROOT / "catalog")
    monkeypatch.setenv("FM_CONFIG", "../instalacion/fleet.yaml")        # relativa: a la raíz del repo
    monkeypatch.setenv("FM_CATALOG", str(tmp_path / "catalog"))         # absoluta: tal cual
    assert install_paths() == (ROOT / "../instalacion/fleet.yaml", tmp_path / "catalog")


def test_queue_por_defecto_y_valores(tmp_path):
    cfg = load_config(_write(tmp_path, YAML), env_path=tmp_path / "no.env")
    assert (cfg.queue.max_size, cfg.queue.max_wait_s) == (50, None)
    cfg = load_config(_write(tmp_path, YAML + "queue: { max_size: 5, max_wait_s: 600 }\n"),
                      env_path=tmp_path / "no.env")
    assert (cfg.queue.max_size, cfg.queue.max_wait_s) == (5, 600.0)


@pytest.mark.parametrize("bad, hint", [
    ("queue: { max_size: 0 }", "max_size"),
    ("queue: { max_size: true }", "max_size"),
    ("queue: { max_wait_s: -1 }", "max_wait_s"),
    ("queue: { prioridad: 1 }", "claves desconocidas"),
    ("queue: 5", "queue"),
])
def test_queue_invalida(tmp_path, bad, hint):
    with pytest.raises(ConfigError, match=hint):
        load_config(_write(tmp_path, YAML + bad + "\n"), env_path=tmp_path / "no.env")
