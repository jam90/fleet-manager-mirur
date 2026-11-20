"""Telemetría del OMRON por su API MQTT (decisión 207), sin broker: los
mensajes se inyectan con `recibir()`, con los payloads vistos en el LD real."""
from __future__ import annotations

import json
import time

import pytest

from fm.adapters.omron.driver import OmronDriver, OmronRobotConfig, parse_config
from fm.adapters.omron.mqtt import DATASTORE, OmronMqtt
from tests.adapters.test_omron_driver import ACCIONES, entorno  # noqa: F401  (fixture)


def _m() -> OmronMqtt:
    return OmronMqtt("omron-1", "127.0.0.1", "apiMonitor", "x")


def _status(m: OmronMqtt, retained=False, **campos):
    d = {"type": "RobotStatus", "status": "Arrived at Taller 2", "mode": "Goal seeking",
         "extended": "Done driving", "wait_state": "Not waiting", "state_of_charge": 90.3,
         "temperature": 40, **campos}
    m.recibir("itk/dt/robot/status", json.dumps(d).encode(), retained)


def _datastore(m: OmronMqtt, **valores):
    base = {"DockingState": "UNDOCKED", "RobotChargeState": "Not", "LaserLocalizationScore": "82"}
    for k, v in {**base, **valores}.items():
        m.recibir(f"itk/dt/dataStore/value/{k}", json.dumps({"id": k, "value": v}).encode())


def _completo(m: OmronMqtt, **ds):
    m.recibir("itk/dt/robot/pose", b'{"x": 8509, "y": -6165, "th": -88}', True)
    _datastore(m, **ds)
    _status(m)


def test_traduce_al_formato_de_status_arcl():
    m = _m()
    _completo(m)
    assert m.al_dia()
    st = m.como_status_arcl()
    assert st["Status"] == "Arrived at Taller 2" and st["ExtendedStatusForHumans"] == "Done driving"
    assert st["StateOfCharge"] == "90.3" and st["Location"] == "8509 -6165 -88"
    assert float(st["LocalizationScore"]) == pytest.approx(0.82)    # DataStore va de 0 a 100


def test_en_el_dock_el_estado_sale_del_datastore():
    # Visto: en el dock, `status` trae el texto para humanos, no "DockingState: …"
    m = _m()
    _completo(m, DockingState="DOCKED", RobotChargeState="Bulk")
    _status(m, status="Charging, Connected to Charging Station DOCK", mode="Dock")
    assert m.como_status_arcl()["Status"] == "DockingState: Docked ChargeState: Bulk"
    _datastore(m, DockingState="DOCKED", RobotChargeState="")
    assert m.como_status_arcl()["Status"] == "DockingState: Docked ChargeState: Not"


def test_retenidos_viejos_y_status_vacio_no_cuentan():
    m = _m()
    m.recibir("itk/dt/robot/pose", b'{"x": 1, "y": 2, "th": 3}', True)
    _datastore(m)
    _status(m, retained=True, status="EStop pressed")      # API colgada: solo retenidos
    assert not m.al_dia()
    _status(m, status="", state_of_charge=0.0)              # el primero tras reiniciar la API
    assert not m.al_dia() and m.status["status"] == "EStop pressed"
    _status(m)
    assert m.al_dia()


def test_sin_status_reciente_deja_de_estar_al_dia(monkeypatch):
    m = _m()
    _completo(m)
    ahora = time.monotonic()
    monkeypatch.setattr("fm.adapters.omron.mqtt.time.monotonic", lambda: ahora + 5)
    assert not m.al_dia()


def test_falta_un_valor_del_datastore():
    m = _m()
    m.recibir("itk/dt/robot/pose", b'{"x": 1, "y": 2, "th": 3}')
    for k in DATASTORE[:-1]:
        m.recibir(f"itk/dt/dataStore/value/{k}", b'{"value": "1"}')
    _status(m)
    assert not m.al_dia()


def test_config_mqtt_desde_env():
    env = {"OMRON_PASSWORD": "arcl", "OMRON_MQTT_PASSWORD": "mq"}
    c = parse_config("omron-1", {"host": "h"}, {}, env)
    assert (c.mqtt_user, c.mqtt_password, c.mqtt_port) == ("apiMonitor", "mq", 8883)
    c = parse_config("omron-1", {"host": "h"}, {}, {**env, "OMRON_MQTT_USER_OMRON_1": "apiControl"})
    assert c.mqtt_user == "apiControl"
    assert parse_config("omron-1", {"host": "h", "mqtt": False}, {}, env).mqtt_password is None
    assert parse_config("omron-1", {"host": "h"}, {}, {"OMRON_PASSWORD": "arcl"}).mqtt_user is None


def test_driver_usa_mqtt_y_vuelve_a_arcl(entorno):  # noqa: F811
    _, robot, srv = entorno
    m = _m()
    m.arrancar = lambda: None                                # sin broker
    d = OmronDriver(OmronRobotConfig("omron-1", "127.0.0.1", "clave", srv.puerto, ACCIONES), mqtt=m)
    try:
        assert d.connect()
        t = d.poll()                                         # sin datos MQTT: ARCL
        assert d.fuente == "ARCL" and t.pose == (0.0, 0.0, 0.0)
        _completo(m, DockingState="UNDOCKED")
        m.recibir("itk/dt/goals", b'{"goals": ["Taller 2", "Almacen", "Nuevo"]}')
        t = d.poll()
        assert d.fuente == "MQTT" and t.battery == pytest.approx(90.3)
        assert t.pose[0] == pytest.approx(8.509) and not t.charging and t.available
        assert "Nuevo" in d.goals
        m._ultimo_status = 0.0                               # deja de llegar
        d.poll()
        assert d.fuente == "ARCL"
    finally:
        d.client.cerrar()
