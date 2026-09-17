import re

from fm.vda5050.header import (HeaderCounter, make_header, now_iso, parse_topic, topic,
                               validate_header)


def test_timestamp_formato_exacto():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", now_iso())


def test_header_id_monotono_por_topic():
    c = HeaderCounter()
    assert [c.next("a"), c.next("a"), c.next("b"), c.next("a")] == [1, 2, 1, 3]


def test_topic_y_parse():
    t = topic("MiR", "mir-1", "state")
    assert t == "vda5050/v3/MiR/mir-1/state"
    p = parse_topic(t)
    assert (p.manufacturer, p.serial, p.subtopic) == ("MiR", "mir-1", "state")
    assert parse_topic("otra/cosa") is None
    assert parse_topic("vda5050/v2/MiR/mir-1/state") is None


def test_header_coincide_con_topic():
    h = make_header(HeaderCounter(), "MiR", "fleet", "order")
    assert validate_header(topic("MiR", "fleet", "order"), h) is None


def test_validate_header_detecta_divergencias():
    h = make_header(HeaderCounter(), "FleetMaster", "mir-1", "order")
    assert "manufacturer" in validate_header("vda5050/v3/MiR/mir-1/order", h)
    h = make_header(HeaderCounter(), "MiR", "", "order")
    assert "serialNumber" in validate_header("vda5050/v3/MiR/fleet/order", h)
    assert "objeto JSON" in validate_header("vda5050/v3/MiR/fleet/order", [1, 2])
    h = make_header(HeaderCounter(), "MiR", "mir-1", "order"); del h["timestamp"]
    assert "timestamp" in validate_header("vda5050/v3/MiR/mir-1/order", h)
    h = make_header(HeaderCounter(), "MiR", "mir-1", "order"); h["version"] = "2.1.0"
    assert "version" in validate_header("vda5050/v3/MiR/mir-1/order", h)
