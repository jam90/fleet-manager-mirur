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
    assert t.errors == []
    # Sin mission: primero el DEBUG; detrás, los de la pestaña Robot (decisión 110)
    assert [i.infoType for i in t.information] == ["MIR_STATUS", "ROBOT_STATE", "DISTANCE_TO_TARGET",
                                                   "ODOMETER", "UPTIME"]
    assert t.information[0].infoLevel == "DEBUG" and "state_id=3" in t.information[0].infoDescriptor


def test_estados_no_disponibles():
    for sid, text in ((11, "Manual"), (12, "Error"), (10, "EmergencyStop")):
        t = to_telemetry(_st(state_id=sid, state_text=text))
        assert not t.available and text in t.unavailable_reason
    assert to_telemetry(_st(state_id=11)).operating_mode == "MANUAL"
    assert to_telemetry(_st(state_id=10)).emergency_stop
    assert to_telemetry(_st(state_id=12)).errors[0].errorType == "MIR_STATE_ERROR"


def test_llave_fisica():
    # Llave en manual: state_id sigue sin ser 11, pero el FM debe verlo (2026-10-09)
    t = to_telemetry(_st(state_id=10, state_text="EmergencyStop", mode_key_state="manual"))
    assert t.operating_mode == "MANUAL" and not t.available
    assert "llave" in t.unavailable_reason and "manual" in t.unavailable_reason
    # Neutra ("idle", visto con mir-2) → INTERVENED; desconocida → modo sin cambiar.
    # En ambos casos no acepta orders (decisiones 58 y 59).
    for key, mode in (("idle", "INTERVENED"), ("otra", "AUTOMATIC")):
        t = to_telemetry(_st(mode_key_state=key))
        assert not t.available and t.operating_mode == mode and f"'{key}'" in t.unavailable_reason
    # "auto" o ausente (firmware que no informa): disponible
    assert to_telemetry(_st(mode_key_state="auto")).available
    assert to_telemetry(_st()).available
    assert "key=auto" in to_telemetry(_st(mode_key_state="auto")).information[0].infoDescriptor


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


def _info(t):
    return {i.infoType: i.infoDescriptor for i in t.information}


def test_info_pestana_robot():
    # Campos de /status que el FM no usa para decidir: van a information[] (decisión 110)
    t = to_telemetry(_st(robot_name="Dejavu", robot_model="MiR250", serial_number="206001015",
                         mode_text="Mission", distance_to_next_target=3.456, moved=8235.02, uptime=7258))
    info = _info(t)
    assert info["ROBOT_NAME"] == "Dejavu" and info["ROBOT_MODEL"] == "MiR250"
    assert info["ROBOT_SERIAL"] == "206001015"
    assert info["ROBOT_STATE"] == "Ready · Mission"
    assert info["DISTANCE_TO_TARGET"] == "3.46" and info["ODOMETER"] == "8235" and info["UPTIME"] == "7258"
    assert all(i.infoLevel == "INFO" for i in t.information if i.infoType != "MIR_STATUS")
    # Lo vacío o en falso no se publica
    for kind in ("USER_PROMPT", "MANUAL_DRIVE", "SAFETY_MUTED", "MAP_UNSAVED"):
        assert kind not in info
    assert "ROBOT_NAME" not in _info(to_telemetry(_st()))


def test_avisos_y_user_prompt():
    t = to_telemetry(_st(joystick_web_session_id="abc", safety_system_muted=True, unloaded_map_changes=True,
                         user_prompt={"question": "¿Pieza cargada?", "options": ["Sí", "No"]}))
    info = _info(t)
    assert info["USER_PROMPT"] == "¿Pieza cargada? [Sí / No]"
    assert {"MANUAL_DRIVE", "SAFETY_MUTED", "MAP_UNSAVED"} <= set(info)
    # Formato sin verificar (decisión 111): si no es el esperado se publica tal cual
    assert _info(to_telemetry(_st(user_prompt="texto suelto")))["USER_PROMPT"] == "texto suelto"


def test_velocidad():
    # Linear en m/s tal cual; angular en grados/s → rad/s (medido con mir-1)
    t = to_telemetry(_st(velocity={"linear": 0.8, "angular": 90.0}))
    assert t.velocity[0] == 0.8 and abs(t.velocity[1] - 1.5707963) < 1e-6
    assert to_telemetry(_st()).velocity == (0.0, 0.0)


def test_error_con_plantilla_legible():
    # Visto en mir-2 el 2026-10-23: la descripción viene como JSON con plantilla
    raw = ('{"message": "Movement timed out, after being blocked a total of %(path_block_timeout)d seconds", '
           '"args": {"path_block_timeout":60}}')
    e = to_telemetry(_st(state_id=12, errors=[{"code": 10101, "description": raw, "module": "MissionController"}])).errors[0]
    assert e.errorDescription == "Movement timed out, after being blocked a total of 60 seconds"
