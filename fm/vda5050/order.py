"""Mensaje `order` VDA 5050 (§6.3): dataclasses + parseo/validación estructural.

Solo se modela lo que el FM usa. La validación aquí es la del *modelo* (tipos,
nodos, edges, released); la de negocio (actionType soportado, parámetros)
vive en `adapters/mir.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fm.vda5050.state import E_VALIDATION_FAILURE


class OrderRejected(Exception):
    """Order no aceptable. `error_type` es un errorType VDA; `description` va
    a `errorDescription` (y a `order_response.errorDescription`)."""

    def __init__(self, error_type: str, description: str):
        self.error_type = error_type
        self.description = description
        super().__init__(f"{error_type}: {description}")


@dataclass
class ActionParameter:
    key: str
    value: Any


@dataclass
class Action:
    actionType: str
    actionId: str
    blockingType: str = "HARD"
    actionDescription: str | None = None
    actionParameters: list[ActionParameter] = field(default_factory=list)

    def params(self) -> dict[str, Any]:
        return {p.key: p.value for p in self.actionParameters}


@dataclass
class Node:
    nodeId: str
    sequenceId: int
    released: bool
    actions: list[Action] = field(default_factory=list)


@dataclass
class Order:
    orderId: str
    orderUpdateId: int
    nodes: list[Node]
    edges: list[dict]
    headerId: int = 0
    manufacturer: str = ""
    serialNumber: str = ""

    def released_actions(self) -> list[Action]:
        return [a for n in self.nodes if n.released for a in n.actions]


def _req(d: dict, key: str, typ, where: str):
    if key not in d:
        raise OrderRejected(E_VALIDATION_FAILURE, f"falta '{key}' en {where}")
    v = d[key]
    if typ is int and isinstance(v, bool) or not isinstance(v, typ):
        raise OrderRejected(E_VALIDATION_FAILURE, f"'{key}' en {where} debe ser {typ.__name__}")
    return v


def parse_order(d: dict) -> Order:
    """dict JSON → `Order`. Lanza `OrderRejected(VALIDATION_FAILURE)` si la
    estructura no cumple lo mínimo (decisión 10 de docs/avance.md)."""
    if not isinstance(d, dict):
        raise OrderRejected(E_VALIDATION_FAILURE, "el payload no es un objeto JSON")
    order_id = _req(d, "orderId", str, "order")
    update_id = _req(d, "orderUpdateId", int, "order")
    raw_nodes = _req(d, "nodes", list, "order")
    raw_edges = _req(d, "edges", list, "order")
    if not raw_nodes:
        raise OrderRejected(E_VALIDATION_FAILURE, "la order no tiene nodos")
    if len(raw_edges) != len(raw_nodes) - 1:
        raise OrderRejected(E_VALIDATION_FAILURE,
                            f"edges ({len(raw_edges)}) debe ser nodes-1 ({len(raw_nodes) - 1})")

    nodes: list[Node] = []
    for i, n in enumerate(raw_nodes):
        where = f"nodes[{i}]"
        if not isinstance(n, dict):
            raise OrderRejected(E_VALIDATION_FAILURE, f"{where} no es un objeto")
        actions = []
        for j, a in enumerate(n.get("actions") or []):
            aw = f"{where}.actions[{j}]"
            params = [ActionParameter(_req(p, "key", str, aw), p.get("value"))
                      for p in (a.get("actionParameters") or [])]
            actions.append(Action(_req(a, "actionType", str, aw), _req(a, "actionId", str, aw),
                                  a.get("blockingType", "HARD"), a.get("actionDescription"), params))
        nodes.append(Node(_req(n, "nodeId", str, where), _req(n, "sequenceId", int, where),
                          _req(n, "released", bool, where), actions))

    if nodes[0].sequenceId != 0:
        raise OrderRejected(E_VALIDATION_FAILURE, "el primer nodo debe tener sequenceId 0")
    if not nodes[0].released:
        raise OrderRejected(E_VALIDATION_FAILURE, "el primer nodo debe estar released")
    if any(not n.released for n in nodes):
        raise OrderRejected(E_VALIDATION_FAILURE,
                            "nodos no released (horizon) no soportados: el MiR no puede retener un horizonte")

    return Order(order_id, update_id, nodes, raw_edges,
                 headerId=int(d.get("headerId", 0)), manufacturer=str(d.get("manufacturer", "")),
                 serialNumber=str(d.get("serialNumber", "")))
