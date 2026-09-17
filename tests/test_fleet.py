"""`Dispatcher` con bus y drivers falsos: topics con manufacturer por robot
y `fleet/*` bajo el nombre de flota (decisión 30)."""
import json

from fm.adapters.base import Job, Telemetry
from fm.config import FleetConfig, RobotConfig
from fm.fleet import Dispatcher
from fm.robot import Robot
from fm.vda5050.header import HeaderCounter, make_header, topic
from fm.vda5050.order import Action

FLEET_MAN = "imperial_fleet"


class FakeBus:
    """Lo que `Dispatcher` usa de `MqttBus`, sin paho."""
    def __init__(self, manufacturers):
        self.manufacturers = {**manufacturers, "fleet": FLEET_MAN}
        self.counter = HeaderCounter()
        self.published: list[tuple[str, dict]] = []
        self.subscriptions: list[str] = []

    def subscribe(self, t, qos=1):
        self.subscriptions.append(t)

    def is_known(self, manufacturer, serial):
        return self.manufacturers.get(serial) == manufacturer

    def publish(self, serial, subtopic, body, retain=False):
        payload = {**make_header(self.counter, self.manufacturers[serial], serial, subtopic), **body}
        self.published.append((topic(self.manufacturers[serial], serial, subtopic), payload))
        return payload


class FakeDriver:
    manufacturer = "MiR"
    last_error = None

    def __init__(self):
        self.executed: list[Job] = []

    def translate(self, action: Action) -> Job:
        return Job(action, f"fake '{action.actionType}'")

    def execute(self, job, priority=0):
        self.executed.append(job)
        return "1"

    def job_status(self, job_id):
        return "RUNNING"


def _order(man, serial, action="coger", order_id="o1"):
    return json.dumps({**make_header(HeaderCounter(), man, serial, "order"),
                       "orderId": order_id, "orderUpdateId": 0, "edges": [],
                       "nodes": [{"nodeId": "N0", "sequenceId": 0, "released": True,
                                  "actions": [{"actionType": action, "actionId": "a1", "blockingType": "HARD"}]}]})


def _setup(manufacturers={"mir-1": "MiR", "ld-1": "OMRON"}):
    cfg = FleetConfig(robots={s: RobotConfig(s, action_types=frozenset({"coger"}))
                              for s in manufacturers})
    robots = {}
    for s, man in manufacturers.items():
        d = FakeDriver(); d.manufacturer = man
        r = Robot(cfg.robots[s], d)
        r.last_telemetry = Telemetry(battery=80.0)
        robots[s] = r
    bus = FakeBus(manufacturers)
    states = []
    disp = Dispatcher(cfg, robots, bus, lambda r: states.append(r.serial))
    return disp, robots, bus, states


def test_suscripcion_generica_y_filtro_por_manufacturer():
    disp, robots, bus, states = _setup()
    disp.subscribe()
    assert bus.subscriptions == ["vda5050/v3/+/+/order", "vda5050/v3/+/+/instantActions"]
    # order a ld-1 bajo su manufacturer → se ejecuta
    disp.handle(topic("OMRON", "ld-1", "order"), _order("OMRON", "ld-1"))
    assert len(robots["ld-1"].driver.executed) == 1 and states == ["ld-1"]
    # el mismo serial bajo otro manufacturer → no es un robot de la flota: se ignora
    disp.handle(topic("MiR", "ld-1", "order"), _order("MiR", "ld-1", order_id="o2"))
    assert len(robots["ld-1"].driver.executed) == 1 and states == ["ld-1"]


def test_fleet_order_bajo_nombre_de_flota():
    disp, robots, bus, states = _setup()
    disp.handle(topic(FLEET_MAN, "fleet", "order"), _order(FLEET_MAN, "fleet"))
    t, resp = bus.published[-1]
    assert t == f"vda5050/v3/{FLEET_MAN}/fleet/order_response"
    assert resp["manufacturer"] == FLEET_MAN and resp["serialNumber"] == "fleet"
    assert resp["status"] == "ASSIGNED" and resp["assignedSerial"] in robots
    # fleet/order bajo el manufacturer de un robot → no existe ese emisor: se ignora
    before = len(bus.published)
    disp.handle(topic("MiR", "fleet", "order"), _order("MiR", "fleet", order_id="o2"))
    assert len(bus.published) == before


def test_header_distinto_del_topic_se_rechaza():
    disp, robots, bus, states = _setup()
    disp.handle(topic(FLEET_MAN, "fleet", "order"), _order("MiR", "fleet"))
    t, resp = bus.published[-1]
    assert resp["status"] == "REJECTED" and resp["errorType"] == "VALIDATION_FAILURE"
