"""Dataclasses del mensaje `state` VDA 5050 v3.0.0 (§7.8) y helpers de errores.

Solo el subconjunto que el FM rellena. `to_dict()` genera exactamente el JSON
que se publica: todos los campos `required` del schema presentes (aunque sean
listas vacías) y los opcionales solo si tienen valor.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# --- enums (strings literales del schema) ---------------------------------
ACTION_STATUS = ("WAITING", "INITIALIZING", "RUNNING", "PAUSED", "RETRIABLE",
                 "FINISHED", "FAILED")
ERROR_LEVELS = ("WARNING", "URGENT", "CRITICAL", "FATAL")
OPERATING_MODES = ("STARTUP", "AUTOMATIC", "SEMIAUTOMATIC", "INTERVENED",
                   "MANUAL", "SERVICE", "TEACH_IN")

# errorType predefinidos en v3 (§6.6.5.4) + extensiones propias (docs/avance.md, decisión 1)
E_VALIDATION_FAILURE = "VALIDATION_FAILURE"
E_INVALID_ORDER_ACTION = "INVALID_ORDER_ACTION"
E_OUTDATED_ORDER_UPDATE = "OUTDATED_ORDER_UPDATE"
E_SAME_ORDER_UPDATE_ID = "SAME_ORDER_UPDATE_ID"
E_OTHER_ORDER_ACTIVE = "OTHER_ORDER_ACTIVE"
E_NO_ROUTE_TO_TARGET = "NO_ROUTE_TO_TARGET"
E_MOBILE_ROBOT_NOT_AVAILABLE = "MOBILE_ROBOT_NOT_AVAILABLE"
E_NO_ORDER_TO_CANCEL = "NO_ORDER_TO_CANCEL"
E_INVALID_INSTANT_ACTION = "INVALID_INSTANT_ACTION"
E_ORDER_UPDATE_FOLLOWING_CANCEL = "ORDER_UPDATE_FOLLOWING_CANCEL"
E_LOCALIZATION_ERROR = "LOCALIZATION_ERROR"
E_NO_MOBILE_ROBOT_AVAILABLE = "NO_MOBILE_ROBOT_AVAILABLE"     # propio: asignador sin candidatos
E_ORDER_EXECUTION_FAILED = "ORDER_EXECUTION_FAILED"           # propio: mission Aborted/Cancelled
E_MIR_UNREACHABLE = "MIR_REST_UNREACHABLE"                    # propio: GET /status falló


def _clean(d: Any) -> Any:
    """Quita claves con valor None (campos opcionales no informados)."""
    if isinstance(d, dict):
        return {k: _clean(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [_clean(v) for v in d]
    return d


@dataclass
class ErrorReference:
    referenceKey: str
    referenceValue: str


@dataclass
class Error:
    errorType: str
    errorLevel: str
    errorDescription: str | None = None
    errorHint: str | None = None
    errorReferences: list[ErrorReference] = field(default_factory=list)


def error_for_order(error_type: str, description: str, order_id: str | None = None,
                    level: str = "WARNING", action_id: str | None = None) -> Error:
    """Error con `errorReferences` apuntando a la order/action afectada."""
    refs = []
    if order_id is not None:
        refs.append(ErrorReference("orderId", order_id))
    if action_id is not None:
        refs.append(ErrorReference("actionId", action_id))
    return Error(error_type, level, description, errorReferences=refs)


@dataclass
class Info:
    infoType: str
    infoLevel: str = "INFO"
    infoDescriptor: str | None = None


@dataclass
class ActionState:
    actionId: str
    actionStatus: str
    actionType: str | None = None
    actionDescriptor: str | None = None
    actionResult: str | None = None


@dataclass
class MobileRobotPosition:
    x: float
    y: float
    theta: float
    mapId: str
    localized: bool = True


@dataclass
class PowerSupply:
    stateOfCharge: float
    charging: bool


@dataclass
class SafetyState:
    activeEmergencyStop: str = "NONE"     # MANUAL | REMOTE | NONE
    fieldViolation: bool = False


@dataclass
class State:
    # header (lo rellena make_header y se mezcla en to_dict)
    headerId: int
    timestamp: str
    version: str
    manufacturer: str
    serialNumber: str
    # cuerpo
    orderId: str = ""
    orderUpdateId: int = 0
    lastNodeId: str = ""
    lastNodeSequenceId: int = 0
    driving: bool = False
    paused: bool = False
    newBaseRequest: bool = False
    operatingMode: str = "AUTOMATIC"
    mobileRobotPosition: MobileRobotPosition | None = None
    powerSupply: PowerSupply = field(default_factory=lambda: PowerSupply(0.0, False))
    safetyState: SafetyState = field(default_factory=SafetyState)
    nodeStates: list = field(default_factory=list)
    edgeStates: list = field(default_factory=list)
    actionStates: list[ActionState] = field(default_factory=list)
    instantActionStates: list[ActionState] = field(default_factory=list)
    zoneActionStates: list = field(default_factory=list)
    errors: list[Error] = field(default_factory=list)
    information: list[Info] = field(default_factory=list)
    maps: list = field(default_factory=list)
    zoneSets: list = field(default_factory=list)
    zoneRequests: list = field(default_factory=list)
    edgeRequests: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return _clean(asdict(self))
