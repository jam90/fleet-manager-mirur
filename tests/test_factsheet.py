"""`factsheet` VDA (§6.11) válido contra el schema oficial, para cada driver."""
import pytest

from fm.adapters.base import ActionInfo
from fm.adapters.mir import MirDriver
from fm.adapters.mir.config import ActionConfig, MirRobotConfig
from fm.adapters.sim import SimDriver, SimRobotConfig
from fm.vda5050.factsheet import build_factsheet, factsheet_for
from fm.vda5050.header import HeaderCounter, make_header
from fm.vda5050.schemas import assert_valid


def _header(man="MiR", serial="mir-1"):
    return make_header(HeaderCounter(), man, serial, "factsheet")


def test_mir_factsheet_valida_y_lista_actions():
    cfg = MirRobotConfig("mir-1", "h", "a", {
        "coger": ActionConfig("coger", "coger"),
        "ir_a": ActionConfig("ir_a", "Ir a posición", position_inputs=["target_pos"], required_inputs=["pieza"]),
    })
    fs = factsheet_for(_header(), MirDriver(cfg), driver_name="mir")
    assert_valid("factsheet", fs)
    assert fs["typeSpecification"]["seriesName"] == "MiR250" and fs["physicalParameters"]["maximumSpeed"] == 2.0
    acts = {a["actionType"]: a for a in fs["protocolFeatures"]["mobileRobotActions"]}
    assert acts["coger"]["actionScopes"] == ["NODE"] and "actionParameters" not in acts["coger"]
    assert [(p["key"], p["valueDataType"], p["isOptional"]) for p in acts["ir_a"]["actionParameters"]] == [
        ("target_pos", "STRING", False), ("pieza", "STRING", False)]
    assert acts["cancelOrder"]["actionScopes"] == ["INSTANT"] and acts["factsheetRequest"]["actionScopes"] == ["INSTANT"]
    assert fs["protocolLimits"]["maximumArrayLengths"]["node.actions"] == 1
    assert fs["manufacturer"] == "MiR" and fs["serialNumber"] == "mir-1"


def test_sim_y_driver_sin_ganchos():
    fs = factsheet_for(_header("SIM", "sim-1"), SimDriver(SimRobotConfig("sim-1", {"coger"})), driver_name="sim")
    assert_valid("factsheet", fs)
    assert fs["typeSpecification"]["seriesName"] == "SIM"

    class Bare:          # driver mínimo: sin describe_actions ni describe_robot
        manufacturer = "X"
    fs = factsheet_for(_header("X", "x-1"), Bare(), action_types=["b", "a"], driver_name="otro")
    assert_valid("factsheet", fs)
    assert fs["typeSpecification"]["seriesName"] == "otro"
    assert [a["actionType"] for a in fs["protocolFeatures"]["mobileRobotActions"]][:2] == ["a", "b"]


def test_build_sin_actions_valida():
    assert_valid("factsheet", build_factsheet(_header(), [], None, "vacio"))
