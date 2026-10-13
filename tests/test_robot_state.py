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
