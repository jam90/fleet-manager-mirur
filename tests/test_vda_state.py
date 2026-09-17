"""El `state` que genera el FM debe validar contra el schema oficial v3.0.0."""
import math

from fm.adapters.mir import StateOverlay, to_vda_state
from fm.mir_client import MirStatus
from fm.vda5050.header import HeaderCounter, make_header
from fm.vda5050.schemas import assert_valid, validation_errors
from fm.vda5050.state import ActionState, Error, error_for_order

STATUS = {
    "state_id": 5, "state_text": "Executing", "battery_percentage": 54.9,
    "battery_time_remaining": 12345, "mode_id": 7, "mode_text": "Mission",
    "position": {"x": 29.8, "y": 7.96, "orientation": -0.8},
    "map_id": "map-guid", "mission_text": "Ir a H2D1", "mission_queue_id": 1283,
    "errors": [],
}


def _header(serial="mir-2"):
    return make_header(HeaderCounter(), "MiR", serial, "state")


def test_state_valida_contra_schema():
    st = MirStatus.from_json(STATUS)
    s = to_vda_state(_header(), st)
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
    d = to_vda_state(_header(), None, rest_error="timeout").to_dict()
    assert_valid("state", d)
    assert d["errors"][0]["errorType"] == "MIR_REST_UNREACHABLE"
    assert "mobileRobotPosition" not in d


def test_state_pause_manual_error_emergencia():
    d = to_vda_state(_header(), MirStatus.from_json({**STATUS, "state_id": 4})).to_dict()
    assert d["paused"] is True and d["driving"] is False
    d = to_vda_state(_header(), MirStatus.from_json({**STATUS, "state_id": 11})).to_dict()
    assert d["operatingMode"] == "MANUAL"
    d = to_vda_state(_header(), MirStatus.from_json({**STATUS, "state_id": 10})).to_dict()
    assert d["safetyState"]["activeEmergencyStop"] == "MANUAL"
    d = to_vda_state(_header(), MirStatus.from_json(
        {**STATUS, "state_id": 12, "errors": [{"code": 10050, "description": "Localization lost", "module": "AMCL"}]})).to_dict()
    assert_valid("state", d)
    assert d["errors"] == [{"errorType": "MIR_10050", "errorLevel": "FATAL",
                            "errorDescription": "Localization lost", "errorHint": "AMCL",
                            "errorReferences": []}]


def test_overlay_order_y_errores_valida():
    ov = StateOverlay(order_id="ord-1", order_update_id=0,
                      action_states=[ActionState("act-1", "RUNNING", "abrir_puerta")],
                      errors=[error_for_order("VALIDATION_FAILURE", "más de una action", "ord-2")],
                      charging=True)
    d = to_vda_state(_header(), MirStatus.from_json(STATUS), ov).to_dict()
    assert_valid("state", d)
    assert d["orderId"] == "ord-1" and d["powerSupply"]["charging"] is True
    assert d["errors"][0]["errorReferences"] == [{"referenceKey": "orderId", "referenceValue": "ord-2"}]


def test_schema_detecta_estado_invalido():
    d = to_vda_state(_header(), MirStatus.from_json(STATUS)).to_dict()
    d["operatingMode"] = "AUTO"
    assert validation_errors("state", d)
