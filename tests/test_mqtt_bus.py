"""`MqttBus` sin broker: topics y headers con manufacturer por robot."""
import json

from fm.mqtt_bus import MqttBus


def test_topics_y_header_por_robot():
    bus = MqttBus("localhost", 1883, {"mir-1": "MiR", "ld-1": "OMRON"}, "imperial_fleet")
    assert bus.topic("mir-1", "state") == "vda5050/v3/MiR/mir-1/state"
    assert bus.topic("ld-1", "state") == "vda5050/v3/OMRON/ld-1/state"
    assert bus.topic("fleet", "order_response") == "vda5050/v3/imperial_fleet/fleet/order_response"
    h = bus.next_header("ld-1", "state")
    assert (h["manufacturer"], h["serialNumber"], h["headerId"]) == ("OMRON", "ld-1", 1)
    assert bus.next_header("mir-1", "state")["headerId"] == 1     # contador por topic
    assert bus.is_known("MiR", "mir-1") and bus.is_known("imperial_fleet", "fleet")
    assert not bus.is_known("MiR", "ld-1") and not bus.is_known("MiR", "fleet")


# ------------------------------------------- un solo FM por broker (decisión 107)
def test_online_entries_solo_cuenta_online():
    from fm.mqtt_bus import online_entries
    msg = lambda state, ts="2026-10-14T08:00:00.000Z": json.dumps({"connectionState": state, "timestamp": ts}).encode()
    assert online_entries({"a/fleet/connection": msg("ONLINE"), "a/mir-1/connection": msg("OFFLINE"),
                           "a/mir-2/connection": msg("CONNECTION_BROKEN"), "a/x/connection": b"no es json"}) \
        == [("a/fleet/connection", "2026-10-14T08:00:00.000Z")]
    assert online_entries({}) == []


def test_run_fm_no_arranca_con_otro_fm_online(monkeypatch, caplog):
    import run_fm
    seen = {}

    def fake_running_fm(host, port, topics, wait=1.0):
        seen["topics"] = topics
        return [("vda5050/v3/imperial_fleet/fleet/connection", "2026-10-14T08:00:00.000Z")]
    monkeypatch.setattr(run_fm, "running_fm", fake_running_fm)
    monkeypatch.setattr(run_fm.MqttBus, "start", lambda self, timeout=10.0: (_ for _ in ()).throw(AssertionError("no debía conectar")))
    assert run_fm.main(["--config", "config/fleet-sim.yaml", "--web-port", "0"]) == 3
    assert "vda5050/v3/imperial_fleet/fleet/connection" in seen["topics"]
    assert "vda5050/v3/SIM/sim-1/connection" in seen["topics"]
    assert "ya hay un Fleet Manager" in caplog.text and "--force" in caplog.text


# ------------------------------- suscripciones al reconectar (decisión 120)
def test_reconectar_renueva_las_suscripciones():
    from fm.mqtt_bus import FLEET
    bus = MqttBus("localhost", 1883, {"mir-1": "MiR"}, "imperial_fleet")
    calls = {"main": [], "presence": []}
    bus._main.subscribe = lambda *a: calls["main"].append(a)
    bus._presence["mir-1"].subscribe = lambda *a: calls["presence"].append(a)

    bus.subscribe("vda5050/v3/+/+/order")
    bus.subscribe("vda5050/v3/+/+/instantActions")
    calls["main"].clear()

    # paho reconecta solo y llama otra vez a on_connect: debe volver a suscribir.
    bus._on_connect(bus._main, FLEET, None, 0)
    assert calls["main"] == [([("vda5050/v3/+/+/order", 1), ("vda5050/v3/+/+/instantActions", 1)],)]
    # Los clientes de presencia no reciben mensajes: no se suscriben a nada.
    bus._on_connect(bus._presence["mir-1"], "mir-1", None, 0)
    assert calls["presence"] == []


def test_conexion_rechazada_no_suscribe():
    from fm.mqtt_bus import FLEET
    bus = MqttBus("localhost", 1883, {"mir-1": "MiR"}, "imperial_fleet")
    calls = []
    bus._main.subscribe = lambda *a: calls.append(a)
    bus.subscribe("vda5050/v3/+/+/order")
    calls.clear()
    bus._on_connect(bus._main, FLEET, None, 5)      # 5 = no autorizado
    assert calls == []
