"""`Robot.state()`: el `state` VDA del robot y el gancho opcional
`extra_state()` del driver (contrato de `fm/adapters/base.py`)."""
from fm.adapters.base import Telemetry
from fm.config import RobotConfig
from fm.robot import Robot
from fm.vda5050.header import HeaderCounter, make_header
from fm.vda5050.schemas import assert_valid
from fm.vda5050.state import Info


class _Driver:
    manufacturer = "FAKE"
    last_error = "timeout"

    def __init__(self, extra=None):
        if extra is not None:
            self.extra_state = extra


def _robot(driver, telemetry):
    r = Robot(RobotConfig("r1"), driver)
    r.last_telemetry = telemetry
    return r


def _header():
    return make_header(HeaderCounter(), "FAKE", "r1", "state")


def test_extra_state_del_driver_se_aplica():
    def extra(s):
        s.information.append(Info("MARCA", "INFO", "dato propio de la marca"))

    s = _robot(_Driver(extra), Telemetry(battery=50)).state(_header())
    assert [i.infoType for i in s.information] == ["MARCA"]
    assert_valid("state", s.to_dict())


def test_sin_gancho_o_sin_telemetria_no_se_llama():
    assert _robot(_Driver(), Telemetry(battery=50)).state(_header()).errors == []
    called = []
    s = _robot(_Driver(lambda s: called.append(1)), None).state(_header())
    assert called == [] and [e.errorType for e in s.errors] == ["ROBOT_UNREACHABLE"]
    assert s.errors[0].errorDescription == "timeout"


def test_extra_state_que_lanza_no_rompe_el_state():
    def extra(s):
        raise KeyError("bug del driver")

    s = _robot(_Driver(extra), Telemetry(battery=50)).state(_header())
    assert s.powerSupply.stateOfCharge == 50
    assert_valid("state", s.to_dict())


def test_fuera_del_site_de_la_flota_no_recibe_orders_y_avisa():
    from fm.assigner import assign
    from fm.config import FleetConfig
    r = _robot(_Driver(), Telemetry(battery=80, site_id="s-otro", site_name="suberri"))
    assert r.available and not r.outside_site                     # flota sin site: no se compara
    r.site = "s-flota"
    assert r.outside_site and not r.available
    s = r.state(_header())
    warn = [e for e in s.errors if e.errorType == "ROBOT_OUTSIDE_SITE"]
    assert len(warn) == 1 and warn[0].errorLevel == "WARNING" and "«suberri»" in warn[0].errorDescription
    assert_valid("state", s.to_dict())
    cfg = FleetConfig(robots={"r1": RobotConfig("r1", action_types=frozenset({"coger"}))})
    res = assign("coger", {"r1": r.snapshot()}, cfg)
    assert res.serial is None and "«suberri»" in res.reason
    r.last_telemetry.site_id = "s-flota"
    assert r.available and not r.outside_site
    r.last_telemetry.site_id = None                               # el driver no lo sabe
    assert r.available


def test_order_dirigida_a_un_robot_fuera_del_site_se_rechaza():
    import pytest
    from fm.vda5050.order import OrderRejected, parse_order
    r = _robot(_Driver(), Telemetry(battery=80, site_id="s-otro"))
    r.site = "s-flota"
    order = parse_order({"headerId": 1, "timestamp": "2026-10-29T10:00:00.000Z", "version": "3.0.0",
                         "manufacturer": "FAKE", "serialNumber": "r1", "orderId": "o1", "orderUpdateId": 0,
                         "nodes": [{"nodeId": "n", "sequenceId": 0, "released": True, "actions": [
                             {"actionType": "coger", "actionId": "a1", "blockingType": "HARD"}]}],
                         "edges": []})
    with pytest.raises(OrderRejected, match="no recibe orders"):
        r.translate_order(order)
