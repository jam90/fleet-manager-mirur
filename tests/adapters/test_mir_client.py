"""Tests sin red del cliente MiR: parseo de /status e índices nombre → GUID."""
import math

from fm.adapters.mir.client import MirApiError, MirClient, MirStatus


def test_status_orientation_grados_a_radianes_normalizados():
    st = MirStatus.from_json({"position": {"x": 1, "y": 2, "orientation": 190}, "state_id": 5})
    assert math.isclose(st.theta, math.radians(-170))
    assert -math.pi <= st.theta <= math.pi
    assert st.state_id == 5


def test_status_tolera_campos_ausentes():
    st = MirStatus.from_json({})
    assert st.battery_percentage == 0.0 and st.errors == [] and st.map_id == ""


class _FakeClient(MirClient):
    """Sustituye las llamadas HTTP por datos fijos."""
    def __init__(self, positions=None, missions=None):
        super().__init__("fake", "Basic x")
        self._positions = positions or []
        self._missions = missions or []

    def positions_get(self):
        return self._positions

    def missions_get(self):
        return self._missions


def test_positions_duplicadas_prefiere_mapa_activo():
    c = _FakeClient(positions=[
        {"name": "Charging station", "guid": "g-otro", "type_id": 20, "map_id": "m-otro"},
        {"name": "Charging station", "guid": "g-activo", "type_id": 20, "map_id": "m-activo"},
        {"name": "H2D1", "guid": "g-h2d1", "type_id": 0, "map_id": "m-activo"},
    ])
    idx = c.index_positions_by_name(active_map_id="m-activo")
    assert idx == {"Charging station": "g-activo", "H2D1": "g-h2d1"}


def test_positions_duplicadas_sin_mapa_se_queda_con_la_primera():
    c = _FakeClient(positions=[
        {"name": "P", "guid": "g1", "type_id": 0},
        {"name": "P", "guid": "g2", "type_id": 0},
    ])
    assert c.index_positions_by_name() == {"P": "g1"}


def test_entry_positions_de_marcadores_se_ignoran():
    """Un VL-marker (11) y un cargador (20) traen su entry (12/21) con el
    mismo nombre: el destino es siempre el marcador (decisión 48)."""
    c = _FakeClient(positions=[
        {"name": "H2D1-VL", "guid": "g-entry", "type_id": 12},
        {"name": "H2D1-VL", "guid": "g-marker", "type_id": 11},
        {"name": "Charging station", "guid": "g-ch-entry", "type_id": 21},
        {"name": "Charging station", "guid": "g-ch", "type_id": 20},
    ])
    assert c.index_positions_by_name() == {"H2D1-VL": "g-marker", "Charging station": "g-ch"}


def test_missions_duplicadas_primera():
    c = _FakeClient(missions=[{"name": "M", "guid": "a"}, {"name": "M", "guid": "b"}])
    assert c.index_missions_by_name() == {"M": "a"}


def test_mir_api_error_incluye_body():
    e = MirApiError("POST", "mission_queue", 400, {"message": "parameter_input_name_not_valid"})
    assert "400" in str(e) and "parameter_input_name_not_valid" in str(e)
