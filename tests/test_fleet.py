"""`Dispatcher` con bus y drivers falsos: topics con manufacturer por robot
y `fleet/*` bajo el nombre de flota (decisión 30)."""
import json

from fm.adapters.base import Job, Telemetry
from fm.config import FleetConfig, RobotConfig
from fm.fleet import Dispatcher
from fm.robot import Robot
from fm.vda5050.header import HeaderCounter, make_header, topic
from fm.vda5050.order import Action, OrderRejected

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


class _BrokenDriver(FakeDriver):
    """`execute` falla como lo haría el MiR: sin red o con un 4xx."""
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def execute(self, job, priority=0):
        raise self.exc


def _setup_broken(exc):
    disp, robots, bus, states = _setup({"mir-1": "MiR"})
    robots["mir-1"].driver = _BrokenDriver(exc)
    return disp, robots, bus, states


def test_fleet_order_con_post_fallido_responde_rejected():
    # Antes la excepción subía hasta drain() y el emisor no recibía order_response.
    for exc, error_type in [(ConnectionError("sin red"), "ROBOT_UNREACHABLE"),
                            (RuntimeError("POST mission_queue → HTTP 400"), "ORDER_EXECUTION_FAILED")]:
        disp, robots, bus, states = _setup_broken(exc)
        disp.handle(topic(FLEET_MAN, "fleet", "order"), _order(FLEET_MAN, "fleet"))
        t, resp = bus.published[-1]
        assert t.endswith("/fleet/order_response")
        assert resp["status"] == "REJECTED" and resp["errorType"] == error_type
        assert "fake 'coger'" in resp["errorDescription"]
        assert robots["mir-1"].orders.active is None and not robots["mir-1"].busy
        # La misma order se puede reintentar: no cuenta como "repetida".
        assert disp.last_fleet_order is None


def test_order_dirigida_con_post_fallido_deja_el_error_en_state():
    disp, robots, bus, states = _setup_broken(ConnectionError("sin red"))
    disp.handle(topic("MiR", "mir-1", "order"), _order("MiR", "mir-1"))
    errs = robots["mir-1"].orders.order_errors
    assert [e.errorType for e in errs] == ["ROBOT_UNREACHABLE"]
    assert errs[0].errorReferences[0].referenceValue == "o1"
    assert states == ["mir-1"] and robots["mir-1"].orders.active is None


# ---------------------------------------------- siguiente candidato (decisión 89)
class _RejectingDriver(FakeDriver):
    """`translate` rechaza como el driver MiR cuando le falta la mission."""
    def __init__(self, error_type="NO_ROUTE_TO_TARGET", description="la mission 'coger' no existe en el robot"):
        super().__init__()
        self.error = OrderRejected(error_type, description)

    def translate(self, action):
        raise self.error


def _setup_two(battery1=90.0, battery2=50.0):
    disp, robots, bus, states = _setup({"mir-1": "MiR", "mir-2": "MiR"})
    robots["mir-1"].last_telemetry = Telemetry(battery=battery1)
    robots["mir-2"].last_telemetry = Telemetry(battery=battery2)
    return disp, robots, bus, states


def _fleet(disp, bus):
    disp.handle(topic(FLEET_MAN, "fleet", "order"), _order(FLEET_MAN, "fleet"))
    return bus.published[-1][1]


def test_si_el_elegido_no_puede_traducir_se_prueba_el_siguiente():
    disp, robots, bus, states = _setup_two()
    robots["mir-1"].driver = _RejectingDriver()          # más batería, pero sin la mission
    resp = _fleet(disp, bus)
    assert resp["status"] == "ASSIGNED" and resp["assignedSerial"] == "mir-2"
    assert len(robots["mir-2"].driver.executed) == 1
    assert robots["mir-1"].orders.active is None


def test_todos_rechazan_por_lo_mismo_conserva_el_error_type():
    disp, robots, bus, states = _setup_two()
    for r in robots.values():
        r.driver = _RejectingDriver()
    resp = _fleet(disp, bus)
    assert resp["status"] == "REJECTED" and resp["errorType"] == "NO_ROUTE_TO_TARGET"
    assert "mir-1: la mission" in resp["errorDescription"]
    assert "mir-2: la mission" in resp["errorDescription"]


def test_todos_rechazan_por_motivos_distintos():
    disp, robots, bus, states = _setup_two()
    robots["mir-1"].driver = _RejectingDriver()
    robots["mir-2"].driver = _RejectingDriver("MOBILE_ROBOT_NOT_AVAILABLE", "índices no cargados")
    resp = _fleet(disp, bus)
    assert resp["status"] == "REJECTED" and resp["errorType"] == "NO_MOBILE_ROBOT_AVAILABLE"
    assert "mir-2: índices no cargados" in resp["errorDescription"]


def test_error_de_la_order_no_prueba_otro_robot():
    # Falta un parámetro: es la order la que está mal, no el robot.
    disp, robots, bus, states = _setup_two()
    robots["mir-1"].driver = _RejectingDriver("VALIDATION_FAILURE", "falta el parámetro obligatorio 'n'")
    resp = _fleet(disp, bus)
    assert resp["status"] == "REJECTED" and resp["errorType"] == "VALIDATION_FAILURE"
    assert robots["mir-2"].driver.executed == []


def test_post_fallido_no_prueba_otro_robot():
    # Tras un timeout el MiR pudo encolarla: no se arriesga a duplicarla.
    disp, robots, bus, states = _setup_two()
    robots["mir-1"].driver = _BrokenDriver(ConnectionError("timeout"))
    resp = _fleet(disp, bus)
    assert resp["status"] == "REJECTED" and resp["errorType"] == "ROBOT_UNREACHABLE"
    assert robots["mir-2"].driver.executed == []


# ---------------------------------------- --validate: schema en runtime (decisión 122)
def _sin_blocking_type(man, serial, order_id="o1"):
    """Pasa `parse_order` (no mira blockingType) pero no el schema oficial."""
    d = json.loads(_order(man, serial, order_id=order_id))
    del d["nodes"][0]["actions"][0]["blockingType"]
    return json.dumps(d)


def test_validate_rechaza_fleet_order_que_no_cumple_el_schema():
    disp, robots, bus, states = _setup()
    disp.validate = True
    disp.handle(topic(FLEET_MAN, "fleet", "order"), _sin_blocking_type(FLEET_MAN, "fleet"))
    t, resp = bus.published[-1]
    assert resp["status"] == "REJECTED" and resp["errorType"] == "VALIDATION_FAILURE"
    assert "schema" in resp["errorDescription"] and "blockingType" in resp["errorDescription"]
    assert not any(r.driver.executed for r in robots.values())


def test_validate_rechaza_order_dirigida_e_instant_actions():
    disp, robots, bus, states = _setup()
    disp.validate = True
    disp.handle(topic("MiR", "mir-1", "order"), _sin_blocking_type("MiR", "mir-1"))
    errs = robots["mir-1"].orders.order_errors
    assert robots["mir-1"].driver.executed == [] and "schema" in errs[0].errorDescription
    # instantActions sin `actions` (obligatorio en el schema)
    ia = json.dumps(make_header(HeaderCounter(), "MiR", "mir-1", "instantActions"))
    disp.handle(topic("MiR", "mir-1", "instantActions"), ia)
    assert "schema" in robots["mir-1"].orders.instant_errors[0].errorDescription


def test_sin_validate_lo_que_admite_parse_order_se_acepta():
    disp, robots, bus, states = _setup()
    disp.handle(topic(FLEET_MAN, "fleet", "order"), _sin_blocking_type(FLEET_MAN, "fleet"))
    assert bus.published[-1][1]["status"] == "ASSIGNED"


def test_motivo_del_schema_acotado():
    from fm.fleet import SCHEMA_PROBLEMS_SHOWN, schema_problem
    d = json.loads(_order(FLEET_MAN, "fleet"))
    for k in ("orderId", "orderUpdateId", "nodes", "edges", "timestamp"):
        del d[k]
    msg = schema_problem("order", d)
    assert msg.count("is a required property") == SCHEMA_PROBLEMS_SHOWN and "(y 2 más)" in msg
    assert schema_problem("order", json.loads(_order(FLEET_MAN, "fleet"))) is None
