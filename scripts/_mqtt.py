"""Helpers MQTT para los scripts: construir orders con el MISMO header que el
servicio (fm.vda5050.header) y publicar/esperar respuestas."""
from __future__ import annotations

import json
import queue
import uuid

import paho.mqtt.client as mqtt

from _common import load
from fm.vda5050.header import HeaderCounter, make_header, topic

_counter = HeaderCounter()


def build_order(manufacturer: str, serial: str, action_type: str, params: dict,
                order_id: str | None = None, update_id: int = 0, node_id: str = "N0") -> dict:
    """Order VDA de un nodo lógico con una action (formato del brief §5.4)."""
    return {
        **make_header(_counter, manufacturer, serial, "order"),
        "orderId": order_id or f"{serial}-{uuid.uuid4().hex[:8]}",
        "orderUpdateId": update_id,
        "nodes": [{"nodeId": node_id, "sequenceId": 0, "released": True,
                   "actions": [{"actionType": action_type, "actionId": "act-1", "blockingType": "HARD",
                                "actionParameters": [{"key": k, "value": v} for k, v in params.items()]}]}],
        "edges": [],
    }


def parse_kv(items: list[str]) -> dict:
    out = {}
    for it in items:
        k, v = it.split("=", 1)
        try:
            v = float(v) if "." in v else int(v)
        except ValueError:
            pass
        out[k] = v
    return out


def connect(cfg) -> mqtt.Client:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.connect(cfg.mqtt.host, cfg.mqtt.port)
    c.loop_start()
    return c


def publish(c: mqtt.Client, t: str, payload: dict, qos: int = 1) -> None:
    c.publish(t, json.dumps(payload), qos=qos).wait_for_publish(5)


def wait_for(c: mqtt.Client, t: str, pred, timeout: float) -> dict | None:
    """Espera el primer mensaje en `t` que cumpla `pred(dict)`."""
    q: queue.Queue = queue.Queue()
    c.message_callback_add(t, lambda cl, ud, m: q.put(m.payload))
    c.subscribe(t, 1)
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            d = json.loads(q.get(timeout=max(0.1, end - time.monotonic())))
        except queue.Empty:
            break
        if pred(d):
            return d
    return None
