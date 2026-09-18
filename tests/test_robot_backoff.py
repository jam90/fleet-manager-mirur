"""Backoff de `Robot.poll()` con el robot inalcanzable (decisión 42)."""
from fm.adapters.base import Telemetry
from fm.config import RobotConfig
from fm.robot import Robot


class FlakyDriver:
    manufacturer = "FAKE"
    last_error = "timeout"

    def __init__(self):
        self.up = False
        self.calls = 0

    def connect(self):
        return True

    def poll(self):
        self.calls += 1
        return Telemetry(battery=50) if self.up else None

    def job_status(self, job_id):
        return None


def test_backoff_exponencial_y_recuperacion():
    t = {"now": 100.0}
    d = FlakyDriver()
    r = Robot(RobotConfig("r1"), d, clock=lambda: t["now"])
    # Fallos: intentos en t=100, 101, 103, 107, 115, 123 (1, 2, 4, 8, 8 s)
    expected_calls = 0
    for now, should_call in [(100, True), (100.5, False), (101, True), (102, False), (103, True),
                             (106, False), (107, True), (114, False), (115, True), (122, False), (123, True)]:
        t["now"] = now
        r.poll()
        expected_calls += should_call
        assert d.calls == expected_calls, now
    assert r.last_telemetry is None and r.poll_failures == 6
    # Vuelve: en el siguiente intento se recupera y se vuelve a preguntar cada tick
    d.up = True
    t["now"] = 131
    assert r.poll() is not None and r.poll_failures == 0
    t["now"] = 132
    r.poll()
    assert d.calls == expected_calls + 2
