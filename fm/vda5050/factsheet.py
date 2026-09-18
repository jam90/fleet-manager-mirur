"""`factsheet` VDA 5050 (§6.11): ficha del robot y del protocolo que soporta.

Lo construye el core a partir de dos ganchos opcionales del driver
(`describe_robot()` → ficha técnica, `describe_actions()` → actionTypes con
sus parámetros) más los límites del propio FM (una action por order, sin
edges, instantActions soportadas). Se publica retained al arrancar y al
recibir `factsheetRequest`.
"""
from __future__ import annotations


from fm.adapters.base import ActionInfo, RobotSpec

# instantActions que atiende el core (fm/instant.py)
INSTANT_ACTIONS = ("startPause", "stopPause", "cancelOrder", "stateRequest", "factsheetRequest")

# Límites del FM, no del robot: los que impone el ciclo de orders (decisiones 10/11).
PROTOCOL_LIMITS = {
    "maximumStringLengths": {"maximumMessageLength": 65536, "maximumIdLength": 128, "idNumericalOnly": False},
    "maximumArrayLengths": {"order.nodes": 1, "order.edges": 0, "node.actions": 1,
                            "instantActions": 8, "actions.actionsParameters": 16},
    "timing": {"minimumOrderInterval": 0.5, "minimumStateInterval": 1.0, "defaultStateInterval": 1.0},
}

# Campos opcionales de order/instantActions que el FM lee. Lo que no está
# aquí se ignora (p.ej. edges, nodePosition: el robot navega con sus missions).
OPTIONAL_PARAMETERS = [
    {"parameter": "order.nodes.actions.actionParameters", "support": "SUPPORTED",
     "description": "se traducen a los inputs de la mission (position_inputs/required_inputs)"},
    {"parameter": "order.nodes.actions.blockingType", "support": "SUPPORTED",
     "description": "se acepta cualquier valor; una sola action por order"},
]


def _generic_spec(name: str) -> RobotSpec:
    return RobotSpec(name, description=f"driver '{name}' sin ficha técnica")


def _action(a: ActionInfo) -> dict:
    return {
        "actionType": a.action_type,
        "actionDescription": a.description or None,
        "actionScopes": ["NODE"],
        "actionParameters": [{"key": p.key, "valueDataType": "STRING", "isOptional": not p.required,
                              "description": f"nombre de position ({len(p.choices)} conocidas)"
                              if p.kind == "position" and p.choices else p.kind}
                             for p in a.params] or None,
        "blockingTypes": ["NONE", "SOFT", "HARD"],
        "pauseAllowed": True,      # startPause
        "cancelAllowed": True,     # cancelOrder
    }


def build_factsheet(header: dict, actions: list[ActionInfo], spec: RobotSpec | None,
                    driver_name: str = "") -> dict:
    spec = spec or _generic_spec(driver_name or header.get("manufacturer", "?"))
    mobile_actions = [_action(a) for a in actions]
    mobile_actions += [{"actionType": t, "actionScopes": ["INSTANT"], "blockingTypes": ["NONE"],
                        "pauseAllowed": False, "cancelAllowed": False} for t in INSTANT_ACTIONS]
    fs = {
        **header,
        "typeSpecification": {
            "seriesName": spec.series_name,
            "seriesDescription": spec.description or None,
            "mobileRobotKinematics": spec.kinematics,
            "mobileRobotClass": spec.robot_class,
            "maximumLoadMass": spec.max_load_kg,
            "localizationTypes": list(spec.localization_types),
            "navigationTypes": list(spec.navigation_types),
        },
        "physicalParameters": {
            "minimumSpeed": spec.min_speed, "maximumSpeed": spec.max_speed,
            "maximumAcceleration": spec.max_acceleration, "maximumDeceleration": spec.max_deceleration,
            "minimumHeight": spec.height, "maximumHeight": spec.height,
            "width": spec.width, "length": spec.length,
        },
        "protocolLimits": PROTOCOL_LIMITS,
        "protocolFeatures": {"optionalParameters": OPTIONAL_PARAMETERS, "mobileRobotActions": mobile_actions},
        "mobileRobotGeometry": {},
        "loadSpecification": {},
    }
    return _clean(fs)


def _clean(d):
    if isinstance(d, dict):
        return {k: _clean(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [_clean(v) for v in d]
    return d


def factsheet_for(header: dict, driver, action_types: list[str] | None = None,
                  driver_name: str = "") -> dict:
    """Factsheet de un robot a partir de su driver (ganchos opcionales)."""
    describe = getattr(driver, "describe_actions", None)
    actions = describe() if describe else [ActionInfo(t) for t in sorted(action_types or [])]
    describe_robot = getattr(driver, "describe_robot", None)
    spec = describe_robot() if describe_robot else None
    return build_factsheet(header, actions, spec, driver_name)
