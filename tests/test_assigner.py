from fm.assigner import RobotSnapshot, assign
from fm.config import FleetConfig, RobotConfig


def _cfg(prefer_not_charging=True):
    acts = frozenset({"coger"})
    return FleetConfig(
        robots={"mir-1": RobotConfig("mir-1", battery_min=25, action_types=acts),
                "mir-2": RobotConfig("mir-2", battery_min=25, action_types=acts)},
        prefer_not_charging=prefer_not_charging)


def snaps(**kw):
    return {s: RobotSnapshot(s, *v) for s, v in kw.items()}


def test_elige_libre_cuando_otro_ocupado():
    r = assign("coger", snaps(**{"mir-1": (76, False), "mir-2": (63, True)}), _cfg())
    assert r.serial == "mir-1" and r.rejections == {"mir-2": "ocupado"}


def test_ranking_mayor_bateria_y_desempate():
    assert assign("coger", snaps(**{"mir-1": (60, False), "mir-2": (70, False)}), _cfg()).serial == "mir-2"
    assert assign("coger", snaps(**{"mir-1": (60, False), "mir-2": (60, False)}), _cfg()).serial == "mir-1"


def test_rechazo_con_detalle():
    r = assign("coger", snaps(**{"mir-1": (76, True), "mir-2": (17, False)}), _cfg())
    assert r.serial is None and r.error_type == "NO_MOBILE_ROBOT_AVAILABLE"
    assert "mir-1: ocupado" in r.reason and "mir-2: batería 17.0% < mínimo 25%" in r.reason


def test_action_no_soportada_y_no_disponible():
    r = assign("volar", snaps(**{"mir-1": (76, False)}), _cfg())
    assert "no soporta 'volar'" in r.reason
    r = assign("coger", {"mir-1": RobotSnapshot("mir-1", 76, False, available=False)}, _cfg())
    assert "no disponible" in r.reason


def test_prefer_not_charging():
    s = {"mir-1": RobotSnapshot("mir-1", 90, False, charging=True),
         "mir-2": RobotSnapshot("mir-2", 50, False)}
    assert assign("coger", s, _cfg()).serial == "mir-2"
    assert assign("coger", s, _cfg(prefer_not_charging=False)).serial == "mir-1"
    # si el que carga es el único, se le asigna
    assert assign("coger", {"mir-1": s["mir-1"]}, _cfg()).serial == "mir-1"
