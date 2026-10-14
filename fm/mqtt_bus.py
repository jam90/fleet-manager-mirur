"""Puente MQTT del FM: publica `state`/`connection`/…, recibe `order`/`instantActions`.

Concurrencia (docs/avance.md, decisión 14): paho ejecuta sus callbacks en su
propio hilo (`loop_start`). Aquí NO se procesa nada en ese hilo: `on_message`
solo encola `(topic, payload_bytes)` en `inbox` y el hilo principal drena la
cola en su tick. Así toda la lógica del FM corre en un único hilo, sin locks.

`connection` (§6.5): Last Will `CONNECTION_BROKEN` fijado ANTES de conectar
(retained, QoS 1); `ONLINE` al conectar; `OFFLINE` al cerrar limpio. Refleja
FM ↔ broker, no FM ↔ MiR.

Un cliente MQTT solo admite UN Last Will, y la norma pide uno por robot. Por
eso hay dos tipos de cliente:
- `_main`: publica state/factsheet/order_response y recibe order/instantActions.
  Su LWT es el del pseudo-serial `fleet`.
- un cliente de *presencia* por robot: solo lleva su LWT y publica su
  `connection`. Si el FM muere, el broker emite `CONNECTION_BROKEN` en cada
  `<serial>/connection` (smoke §13.9).

**Un solo FM por broker** (decisión 107): antes de conectar, `running_fm()`
mira los `connection` retenidos; si alguno dice `ONLINE`, ya hay un FM vivo
(uno parado deja `OFFLINE`; uno muerto, `CONNECTION_BROKEN`) y `run_fm` no
arranca. Dos FM a la vez se echan del broker (mismo client id) y, si no, los
dos atenderían cada `fleet/order`: el mismo trabajo a dos robots.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from typing import Callable

import paho.mqtt.client as mqtt

from fm.vda5050.header import HeaderCounter, make_header, topic

log = logging.getLogger("fm.mqtt")

# QoS según §4.1: 1 solo para connection; el resto 0. order_response es
# extensión propia y se publica con QoS 1 para no perder el ACK.
QOS = {"state": 0, "factsheet": 0, "visualization": 0, "connection": 1, "order_response": 1}
FLEET = "fleet"


class MqttBus:
    def __init__(self, host: str, port: int, manufacturers: dict[str, str],
                 fleet_manufacturer: str, client_id: str = "fleet-manager"):
        """`manufacturers`: serial → segmento <manufacturer> de sus topics (el
        del driver de cada robot). `fleet/*` va bajo `fleet_manufacturer`."""
        self.host, self.port = host, port
        self.manufacturers = {**manufacturers, FLEET: fleet_manufacturer}
        self.serials = list(manufacturers)
        self.counter = HeaderCounter()
        self.inbox: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self._ready: dict[str, threading.Event] = {}
        self._stopping = False           # cierre pedido: la desconexión no es un fallo
        # Espejo de publicaciones (topic, payload) para la interfaz web; None = sin UI.
        self.on_publish: Callable[[str, dict], None] | None = None

        self._main = self._new_client(f"{client_id}", FLEET)
        self._main.on_message = self._on_message
        self._presence = {s: self._new_client(f"{client_id}-{s}", s) for s in self.serials}

    def _new_client(self, client_id: str, serial: str) -> mqtt.Client:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                        protocol=mqtt.MQTTv311, clean_session=True)
        c.user_data_set(serial)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        # El LWT lleva header propio; el headerId lo pone el broker "en nuestro
        # nombre", así que se consume un número del contador de ese topic.
        c.will_set(self.topic(serial, "connection"),
                   json.dumps(self._connection_payload(serial, "CONNECTION_BROKEN")),
                   qos=1, retain=True)
        self._ready[serial] = threading.Event()
        return c

    def _client_for(self, serial: str) -> mqtt.Client:
        return self._presence.get(serial, self._main)

    def manufacturer_of(self, serial: str) -> str:
        return self.manufacturers[serial]

    def topic(self, serial: str, subtopic: str) -> str:
        return topic(self.manufacturer_of(serial), serial, subtopic)

    def is_known(self, manufacturer: str, serial: str) -> bool:
        """¿(manufacturer, serial) del topic corresponde a un robot configurado o a `fleet`?"""
        return self.manufacturers.get(serial) == manufacturer

    # ------------------------------------------------------------ ciclo vida
    def connection_topics(self) -> list[str]:
        """Los `connection` que publicaría este FM: `fleet` y sus robots."""
        return [self.topic(s, "connection") for s in [FLEET, *self.serials]]

    def start(self, timeout: float = 10.0) -> None:
        for c in [self._main, *self._presence.values()]:
            c.connect(self.host, self.port, keepalive=30)
            c.loop_start()
        for serial, ev in self._ready.items():
            if not ev.wait(timeout):
                raise TimeoutError(f"[{serial}] sin conexión con el broker {self.host}:{self.port}")

    def stop(self) -> None:
        """Cierre limpio: OFFLINE en todos los `connection` y desconexión."""
        self._stopping = True
        for serial in [*self.serials, FLEET]:
            info = self.publish_connection(serial, "OFFLINE")
            try:
                info.wait_for_publish(2.0)   # que salga el QoS 1 antes de cortar
            except Exception:
                pass
        for c in [self._main, *self._presence.values()]:
            c.loop_stop()
            c.disconnect()

    def subscribe(self, t: str, qos: int = 1) -> None:
        self._main.subscribe(t, qos)
        log.info("[mqtt] suscrito a %s", t)

    # -------------------------------------------------------------- publicar
    def next_header(self, serial: str, subtopic: str) -> dict:
        """Header para quien construye el payload por su cuenta (p.ej. `State`)."""
        return make_header(self.counter, self.manufacturer_of(serial), serial, subtopic)

    def publish_raw(self, serial: str, subtopic: str, payload: dict,
                    retain: bool = False) -> mqtt.MQTTMessageInfo:
        """Publica un payload que YA lleva header (generado con `next_header`)."""
        client = self._client_for(serial) if subtopic == "connection" else self._main
        t = self.topic(serial, subtopic)
        if self.on_publish is not None:
            self.on_publish(t, payload)
        return client.publish(t, json.dumps(payload), qos=QOS.get(subtopic, 0), retain=retain)

    def publish(self, serial: str, subtopic: str, body: dict, retain: bool = False) -> dict:
        """Añade el header (≡ topic) y publica. Devuelve el payload completo."""
        payload = {**self.next_header(serial, subtopic), **body}
        self.publish_raw(serial, subtopic, payload, retain)
        return payload

    def publish_connection(self, serial: str, connection_state: str) -> mqtt.MQTTMessageInfo:
        return self.publish_raw(serial, "connection",
                                self._connection_payload(serial, connection_state), retain=True)

    def _connection_payload(self, serial: str, connection_state: str) -> dict:
        return {**self.next_header(serial, "connection"), "connectionState": connection_state}

    # ------------------------------------------------------------- callbacks
    def _on_connect(self, client, serial, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error("[%s] conexión MQTT rechazada: %s", serial, reason_code)
            return
        log.info("[%s] conectado al broker %s:%d", serial, self.host, self.port)
        self.publish_connection(serial, "ONLINE")
        self._ready[serial].set()

    def _on_disconnect(self, client, serial, flags, reason_code, properties=None):
        self._ready[serial].clear()
        if self._stopping:
            log.info("[%s] MQTT desconectado (cierre del FM)", serial)
        else:
            log.warning("[%s] MQTT desconectado (%s); paho reintentará", serial, reason_code)

    def _on_message(self, client, userdata, msg):
        self.inbox.put((msg.topic, msg.payload))


# ------------------------------------------------- ¿hay ya un FM en marcha?
def online_entries(retained: dict[str, bytes]) -> list[tuple[str, str]]:
    """`connection` retenidos (topic → payload) que dicen `ONLINE`, con su
    `timestamp`. Función pura: es lo que decide si `run_fm` arranca."""
    out = []
    for t, payload in sorted(retained.items()):
        try:
            d = json.loads(payload)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("connectionState") == "ONLINE":
            out.append((t, str(d.get("timestamp", "?"))))
    return out


def running_fm(host: str, port: int, topics: list[str], wait: float = 1.0) -> list[tuple[str, str]] | None:
    """Lee los retenidos de `topics` con un cliente de usar y tirar (client id
    propio, para no echar a nadie) y devuelve los que están `ONLINE`. None si
    no se pudo preguntar al broker: entonces decide el arranque normal."""
    retained: dict[str, bytes] = {}
    done = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            client.subscribe([(t, 1) for t in topics])
        done.set()

    def on_message(client, userdata, msg):
        if msg.retain:
            retained[msg.topic] = msg.payload

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"fleet-manager-probe-{os.getpid()}",
                    protocol=mqtt.MQTTv311, clean_session=True)
    c.on_connect, c.on_message = on_connect, on_message
    try:
        c.connect(host, port, keepalive=10)
    except OSError as e:
        log.warning("no se pudo comprobar si hay otro FM (%s); se sigue", e)
        return None
    c.loop_start()
    try:
        if not done.wait(3.0):
            return None
        time.sleep(wait)                  # el broker manda los retenidos al suscribirse
    finally:
        c.disconnect()
        c.loop_stop()
    return online_entries(retained)
