"""`MqttBus` sin broker: topics y headers con manufacturer por robot."""
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
