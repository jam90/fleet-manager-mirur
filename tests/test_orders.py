import pytest

from fm.adapters.base import Job, Telemetry
from fm.adapters.mir.translate import from_vda_order
from fm.config import ActionConfig, RobotConfig
from fm.orders import IgnoreOrder, OrderTracker, pick_action
from fm.vda5050.order import OrderRejected, parse_order

ROBOT = RobotConfig("mir-1", "h", "a", 25, {
    "coger": ActionConfig("coger", "coger"),
    "ir_a": ActionConfig("ir_a", "Ir a posición", position_inputs=["target_pos"], required_inputs=["pieza"]),
})
MISSIONS = {"coger": "g-coger", "Ir a posición": "g-ir"}
POSITIONS = {"H2D1-VL": "g-h2d1"}
INPUTS = {"g-ir": {"target_pos", "pieza", "extra"}}


def order(action_type="coger", params=None, order_id="o1", update_id=0, actions=None, released=True):
    acts = actions if actions is not None else [
        {"actionType": action_type, "actionId": "a1", "blockingType": "HARD",
         "actionParameters": [{"key": k, "value": v} for k, v in (params or {}).items()]}]
    return parse_order({"headerId": 1, "orderId": order_id, "orderUpdateId": update_id,
                        "nodes": [{"nodeId": "N0", "sequenceId": 0, "released": released, "actions": acts}],
                        "edges": []})


def ready():
    return Telemetry(battery=50.0)


def test_parse_valida_estructura():
    with pytest.raises(OrderRejected, match="edges"):
        parse_order({"orderId": "o", "orderUpdateId": 0, "nodes": [{}], "edges": [{}]})
    with pytest.raises(OrderRejected, match="released"):
        order(released=False)
    with pytest.raises(OrderRejected, match="orderUpdateId"):
        parse_order({"orderId": "o", "orderUpdateId": "0", "nodes": [], "edges": []})


def test_una_sola_action():
    o = order(actions=[{"actionType": "coger", "actionId": "a1"}, {"actionType": "dejar", "actionId": "a2"}])
    with pytest.raises(OrderRejected, match="solo se admite una action"):
        pick_action(o)
    assert pick_action(o, {"a1"}).actionId == "a2"


def test_traduccion_sin_parametros():
    req = from_vda_order(pick_action(order()), ROBOT, MISSIONS, POSITIONS, INPUTS)
    assert req.mission_guid == "g-coger" and req.parameters == []


def test_traduccion_con_position_y_required_y_extra():
    o = order("ir_a", {"target_pos": "H2D1-VL", "pieza": 0, "extra": "x", "ignorado": 1})
    req = from_vda_order(pick_action(o), ROBOT, MISSIONS, POSITIONS, INPUTS)
    assert req.parameters == [{"id": "target_pos", "value": "g-h2d1"}, {"id": "pieza", "value": 0},
                              {"id": "extra", "value": "x"}]


def test_errores_de_traduccion():
    with pytest.raises(OrderRejected) as e:
        from_vda_order(pick_action(order("volar")), ROBOT, MISSIONS, POSITIONS, INPUTS)
    assert e.value.error_type == "INVALID_ORDER_ACTION"
    with pytest.raises(OrderRejected) as e:
        from_vda_order(pick_action(order("ir_a", {"pieza": 1})), ROBOT, MISSIONS, POSITIONS, INPUTS)
    assert e.value.error_type == "VALIDATION_FAILURE" and "target_pos" in e.value.description
    with pytest.raises(OrderRejected) as e:
        from_vda_order(pick_action(order("ir_a", {"target_pos": "NoExiste", "pieza": 1})), ROBOT, MISSIONS, POSITIONS, INPUTS)
    assert e.value.error_type == "NO_ROUTE_TO_TARGET"
    with pytest.raises(OrderRejected) as e:
        from_vda_order(pick_action(order("ir_a", {"target_pos": "H2D1-VL", "pieza": 1})), ROBOT, MISSIONS, POSITIONS, INPUTS, allowlist={"H2D2-VL"})
    assert e.value.error_type == "NO_ROUTE_TO_TARGET"
    with pytest.raises(OrderRejected) as e:
        from_vda_order(pick_action(order()), ROBOT, {}, POSITIONS, INPUTS)
    assert e.value.error_type == "NO_ROUTE_TO_TARGET"


class FakeDriver:
    """Solo `job_status`: devuelve la secuencia dada y repite el último."""
    def __init__(self, states):
        self.states = list(states)

    def job_status(self, job_id):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


def _accept(t, o):
    req = from_vda_order(pick_action(o), ROBOT, MISSIONS, POSITIONS, INPUTS)
    job = Job(req.action, f"mission '{req.mission_name}'", req)
    t.accept(o, job, "42")
    return job


def test_ciclo_order_update_id():
    t = OrderTracker("mir-1")
    o = order()
    t.check_new(o, ready())
    _accept(t, o)
    with pytest.raises(IgnoreOrder):
        t.check_new(order(), ready())
    with pytest.raises(OrderRejected) as e:
        t.check_new(order(order_id="o2"), ready())
    assert e.value.error_type == "OTHER_ORDER_ACTIVE"
    with pytest.raises(OrderRejected) as e:
        t.check_new(order(update_id=1), ready())
    assert e.value.error_type == "VALIDATION_FAILURE"
    # termina → se admite otra order y también un update; regresión sigue rechazada
    t.poll(FakeDriver(["FINISHED"]))
    assert not t.busy and t.overlay().action_states[0].actionStatus == "FINISHED"
    t.check_new(order(order_id="o2"), ready())
    t.check_new(order(update_id=1), ready())
    _accept(t, order(update_id=3))
    with pytest.raises(OrderRejected) as e:
        t.check_new(order(update_id=2), ready())
    assert e.value.error_type == "OUTDATED_ORDER_UPDATE"


def test_robot_no_disponible_y_fallo_ejecucion():
    t = OrderTracker("mir-1")
    with pytest.raises(OrderRejected) as e:
        t.check_new(order(), Telemetry(battery=50.0, available=False, unavailable_reason="Manual"))
    assert e.value.error_type == "MOBILE_ROBOT_NOT_AVAILABLE" and "Manual" in e.value.description
    _accept(t, order())
    t.poll(FakeDriver(["RUNNING", "FAILED"]))
    assert t.overlay().action_states[0].actionStatus == "RUNNING" and t.busy
    t.poll(FakeDriver([None]))     # sin respuesta: se conserva el estado
    assert t.overlay().action_states[0].actionStatus == "RUNNING" and t.busy
    t.poll(FakeDriver(["FAILED"]))
    ov = t.overlay()
    assert ov.action_states[0].actionStatus == "FAILED" and not t.busy
    assert ov.errors[0].errorType == "ORDER_EXECUTION_FAILED"
    assert {r.referenceKey for r in ov.errors[0].errorReferences} == {"orderId", "actionId"}


def test_rechazo_se_limpia_al_aceptar():
    t = OrderTracker("mir-1")
    t.reject("o0", OrderRejected("VALIDATION_FAILURE", "x"))
    assert t.overlay().errors[0].errorReferences[0].referenceValue == "o0"
    _accept(t, order())
    assert t.overlay().errors == []
