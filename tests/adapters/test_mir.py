"""Traducción MiR → core: `MirStatus` → `Telemetry` (sin red)."""
from fm.adapters.mir.translate import to_telemetry
from fm.adapters.mir.client import MirStatus

BASE = {"state_id": 3, "state_text": "Ready", "battery_percentage": 61.0,
        "position": {"x": 1.0, "y": 2.0, "orientation": 90.0}, "map_id": "m",
        "mission_text": "", "mission_queue_id": None, "errors": []}


def _st(**over):
    return MirStatus.from_json({**BASE, **over})


def test_ready_disponible_y_libre():
    t = to_telemetry(_st())
    assert t.available and not t.foreign_busy and not t.driving and not t.paused
    assert t.battery == 61.0 and t.map_id == "m" and t.pose[:2] == (1.0, 2.0)
    assert t.operating_mode == "AUTOMATIC" and not t.emergency_stop
    assert t.charging is None          # el MiR no lo dice: decide el overlay
    assert t.errors == [] and t.information == []


def test_estados_no_disponibles():
    for sid, text in ((11, "Manual"), (12, "Error"), (10, "EmergencyStop")):
        t = to_telemetry(_st(state_id=sid, state_text=text))
        assert not t.available and text in t.unavailable_reason
    assert to_telemetry(_st(state_id=11)).operating_mode == "MANUAL"
    assert to_telemetry(_st(state_id=10)).emergency_stop
    assert to_telemetry(_st(state_id=12)).errors[0].errorType == "MIR_STATE_ERROR"


def test_mission_propia_vs_ajena():
    # Executing con mission_queue 7: ajena si el FM no lanzó la 7 (decisión 16)
    st = _st(state_id=5, state_text="Executing", mission_queue_id=7, mission_text="Ir a H2D1")
    assert to_telemetry(st).foreign_busy
    assert to_telemetry(st, {3}).foreign_busy
    t = to_telemetry(st, {7})
    assert not t.foreign_busy and t.driving
    assert t.information[0].infoDescriptor == "Ir a H2D1"
    # En Pause con una mission en cola también cuenta como ocupado
    assert to_telemetry(_st(state_id=4, mission_queue_id=9)).foreign_busy
    assert to_telemetry(_st(state_id=4, mission_queue_id=9)).paused


def test_errores_mir_a_vda():
    st = _st(state_id=12, errors=[{"code": 10050, "description": "Localization lost", "module": "AMCL"}])
    e = to_telemetry(st).errors
    assert [(x.errorType, x.errorLevel, x.errorHint) for x in e] == [("MIR_10050", "FATAL", "AMCL")]
    st = _st(state_id=3, errors=[{"code": 1, "description": "warn"}])
    assert to_telemetry(st).errors[0].errorLevel == "URGENT"
