"""El `state` que genera el FM debe validar contra el schema oficial v3.0.0."""
import math

from fm.adapters.base import Telemetry
from fm.adapters.mir import to_telemetry
from fm.mir_client import MirStatus
from fm.vda5050.header import HeaderCounter, make_header
from fm.vda5050.schemas import assert_valid, validation_errors
from fm.vda5050.state import ActionState, error_for_order
from fm.vda5050.state_builder import StateOverlay, to_vda_state

STATUS = {
    "state_id": 5, "state_text": "Executing", "battery_percentage": 54.9,
    "battery_time_remaining": 12345, "mode_id": 7, "mode_text": "Mission",
    "position": {"x": 29.8, "y": 7.96, "orientation": -0.8},
    "map_id": "map-guid", "mission_text": "Ir a H2D1", "mission_queue_id": 1283,
    "errors": [],
}


def _header(serial="mir-2"):
    return make_header(HeaderCounter(), "MiR", serial, "state")


def _tel(**over):
    """Telemetría MiR de ejemplo, pasando por el traductor real del adapter."""
    return to_telemetry(MirStatus.from_json({**STATUS, **over}))


def test_state_valida_contra_schema():
    s = to_vda_state(_header(), _tel())
    d = s.to_dict()
    assert_valid("state", d)
    assert d["driving"] is True and d["paused"] is False
    assert d["operatingMode"] == "AUTOMATIC"
    assert d["mobileRobotPosition"]["mapId"] == "map-guid"
    assert math.isclose(d["mobileRobotPosition"]["theta"], math.radians(-0.8))
    assert d["powerSupply"] == {"stateOfCharge": 54.9, "charging": False}
    assert d["information"] == [{"infoType": "MISSION", "infoLevel": "INFO", "infoDescriptor": "Ir a H2D1"}]
    assert d["safetyState"] == {"activeEmergencyStop": "NONE", "fieldViolation": False}
    # header ≡ topic
    assert d["manufacturer"] == "MiR" and d["serialNumber"] == "mir-2" and d["headerId"] == 1


def test_state_sin_rest_valida_y_lleva_error():
    d = to_vda_state(_header(), None, error="timeout").to_dict()
    assert_valid("state", d)
    assert d["errors"][0] == {"errorType": "ROBOT_UNREACHABLE", "errorLevel": "URGENT",
                              "errorDescription": "timeout", "errorReferences": []}
    assert "mobileRobotPosition" not in d


def test_state_pause_manual_error_emergencia():
    d = to_vda_state(_header(), _tel(state_id=4)).to_dict()
    assert d["paused"] is True and d["driving"] is False
    d = to_vda_state(_header(), _tel(state_id=11)).to_dict()
    assert d["operatingMode"] == "MANUAL"
    d = to_vda_state(_header(), _tel(state_id=10)).to_dict()
    assert d["safetyState"]["activeEmergencyStop"] == "MANUAL"
    d = to_vda_state(_header(), _tel(
        state_id=12, errors=[{"code": 10050, "description": "Localization lost", "module": "AMCL"}])).to_dict()
    assert_valid("state", d)
    assert d["errors"] == [{"errorType": "MIR_10050", "errorLevel": "FATAL",
                            "errorDescription": "Localization lost", "errorHint": "AMCL",
                            "errorReferences": []}]


def test_overlay_order_y_errores_valida():
    ov = StateOverlay(order_id="ord-1", order_update_id=0,
                      action_states=[ActionState("act-1", "RUNNING", "abrir_puerta")],
                      errors=[error_for_order("VALIDATION_FAILURE", "más de una action", "ord-2")],
                      charging=True)
    d = to_vda_state(_header(), _tel(), ov).to_dict()
    assert_valid("state", d)
    assert d["orderId"] == "ord-1" and d["powerSupply"]["charging"] is True
    assert d["errors"][0]["errorReferences"] == [{"referenceKey": "orderId", "referenceValue": "ord-2"}]


def test_telemetria_minima_sin_pose_valida():
    """Un driver que no sabe posicionarse (p.ej. sim) debe poder publicar state."""
    d = to_vda_state(_header(), Telemetry(battery=80.0, charging=True)).to_dict()
    assert_valid("state", d)
    assert "mobileRobotPosition" not in d
    assert d["powerSupply"] == {"stateOfCharge": 80.0, "charging": True}


def test_schema_detecta_estado_invalido():
    d = to_vda_state(_header(), _tel()).to_dict()
    d["operatingMode"] = "AUTO"
    assert validation_errors("state", d)
