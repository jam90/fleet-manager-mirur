"""Telemetría de un OMRON por su API MQTT (FLOW Core ≥ 4.1, puerto 8883).

Por qué: con ARCL el driver tiene que preguntar `status` en cada tick y
esperar la respuesta (0,2–1 s por wifi). La API MQTT del robot publica sola
el estado, la posición y la cola, así que el tick solo lee lo último que
llegó. Las órdenes siguen yendo por ARCL (7171): el manual (M107) dice que la
API MQTT no sustituye a ARCL para controlar el robot, y sus respuestas a
comandos ARCL llegan sin el id de la petición, mezcladas con el resto.

Lo visto en el LD real (2026-11-20, decisión 207):
- Broker en el propio robot, TLS 1.2 con certificado autofirmado: hay que
  conectar sin validarlo. Usuarios `apiControl` (lee y escribe) o
  `apiMonitor` (solo lee); se crean en SetNetGo. Aquí solo se lee: basta
  `apiMonitor`, aunque los centros suelen tener solo `apiControl`.
- `itk/dt/robot/status`: unas 5 veces por segundo aunque nada cambie. Su
  `status` es el `Status:` de ARCL salvo en el dock, donde trae el texto
  para humanos ("Charging, Connected to Charging Station DOCK"); por eso el
  estado del dock se toma del DataStore (`DockingState`, `RobotChargeState`).
- `itk/dt/robot/pose`: x, y en mm y θ en grados, solo al moverse.
- `itk/dt/goals`: la lista de goals del mapa, al cambiar.
- DataStore: hay que pedir cada valor (`itk/cmd/dataStore/subscribe/req/<n>`
  con un intervalo) y llega en `itk/dt/dataStore/value/<n>`.
  `LaserLocalizationScore` va de 0 a 100 (ARCL lo da de 0 a 1).
- Tras reiniciar la aplicación MQTT API, el primer `status` llega vacío y con
  batería 0.0: se descarta.
- Si la aplicación MQTT API se cuelga (pasó tras pulsar la seta), el broker
  sigue aceptando conexiones y solo entrega los retenidos, con datos viejos.
  El reloj del robot iba una hora atrasado, así que la edad no se mide con
  su `upd`, sino con la hora local de llegada, y los retenidos no cuentan.
"""
from __future__ import annotations

import json
import logging
import ssl
import time

import paho.mqtt.client as mqtt

log = logging.getLogger("fm.omron")

# Valores del DataStore que se piden al conectar, y cada cuánto.
DATASTORE = ("DockingState", "RobotChargeState", "LaserLocalizationScore")
INTERVALO_DATASTORE = "1s"

# Sin `status` nuevo en este tiempo, los datos MQTT no valen y el driver
# vuelve a preguntar por ARCL. Llegan ~5 por segundo: 2 s es mucho margen.
MAX_EDAD_S = 2.0


class OmronMqtt:
    """Suscripción de solo lectura a la API MQTT de UN robot.

    paho la atiende en su hilo y aquí solo se guardan los últimos valores
    (asignaciones simples, sin locks: el hilo de `poll()` solo los lee).
    """

    def __init__(self, serial: str, host: str, user: str, password: str, port: int = 8883):
        self.serial, self.host, self.port = serial, host, port
        self.pose: dict | None = None
        self.status: dict | None = None
        self.goals: list[str] | None = None
        self.datastore: dict[str, str] = {}
        self._ultimo_status = 0.0          # time.monotonic() del último status en vivo
        self._cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                    client_id=f"fm-{serial}-{int(time.time())}")
        self._cliente.username_pw_set(user, password)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # certificado autofirmado del robot
        self._cliente.tls_set_context(ctx)
        self._cliente.on_connect = self._al_conectar
        self._cliente.on_message = lambda c, u, m: self.recibir(m.topic, m.payload, m.retain)

    # --- conexión ---

    def arrancar(self) -> None:
        """Conecta en segundo plano; paho reintenta solo si se cae."""
        # Keepalive entre 60 y 120 s: con más, el broker del robot desconecta (M107 §4-1-2)
        self._cliente.connect_async(self.host, self.port, keepalive=60)
        self._cliente.loop_start()

    def parar(self) -> None:
        self._cliente.loop_stop()
        self._cliente.disconnect()

    def _al_conectar(self, c, u, flags, rc, props=None) -> None:
        if rc != 0:
            log.warning("[%s] MQTT %s:%d rechaza la conexión: %s", self.serial, self.host, self.port, rc)
            return
        log.info("[%s] MQTT conectado a %s:%d", self.serial, self.host, self.port)
        # Topics concretos, sin '#': lo pide el manual para no cargar el broker del robot
        c.subscribe([("itk/dt/robot/status", 0), ("itk/dt/robot/pose", 0),
                     ("itk/dt/goals", 0), ("itk/dt/dataStore/value/+", 0)])
        # Las suscripciones al DataStore se piden en cada conexión: no sabemos
        # si el robot las recuerda al reiniciar la aplicación
        for nombre in DATASTORE:
            c.publish(f"itk/cmd/dataStore/subscribe/req/{nombre}",
                      json.dumps({"interval": INTERVALO_DATASTORE}), qos=1)

    # --- mensajes (público para poder probarlo sin broker) ---

    def recibir(self, topic: str, payload: bytes, retained: bool = False) -> None:
        try:
            d = json.loads(payload)
        except ValueError:
            return
        if topic == "itk/dt/robot/status":
            if not d.get("status") and not d.get("state_of_charge"):
                return                      # el primero tras reiniciar la API, vacío
            self.status = d
            if not retained:
                self._ultimo_status = time.monotonic()
        elif topic == "itk/dt/robot/pose":
            self.pose = d
        elif topic == "itk/dt/goals":
            self.goals = [str(g) for g in d.get("goals") or []]
        elif topic.startswith("itk/dt/dataStore/value/"):
            self.datastore[topic.rsplit("/", 1)[1]] = str(d.get("value", ""))

    # --- lectura desde el driver ---

    def al_dia(self) -> bool:
        """Hay status en vivo reciente, pose y lo que hace falta del DataStore."""
        return (time.monotonic() - self._ultimo_status < MAX_EDAD_S and self.pose is not None
                and all(n in self.datastore for n in DATASTORE))

    def como_status_arcl(self) -> dict[str, str]:
        """Lo recibido, con las claves y formatos de `status` de ARCL, para
        que el driver lo trate igual venga de donde venga."""
        st, ds, p = self.status or {}, self.datastore, self.pose or {}
        estado = st.get("status", "")
        dock = ds.get("DockingState", "").capitalize()       # DOCKED → Docked
        if dock in ("Docked", "Docking", "Undocking"):
            # ARCL pone aquí la línea del dock; ForcedState no lo da el DataStore.
            # Visto "Bulk" cargando; sin cargar, supuesto "Not" (como en ARCL)
            carga = ds.get("RobotChargeState", "").strip()
            carga = "Not" if not carga or carga.lower().startswith("not") else carga.split()[0]
            estado = f"DockingState: {dock} ChargeState: {carga}"
        try:
            score = f"{float(ds.get('LaserLocalizationScore', 0)) / 100:.6f}"
        except ValueError:
            score = "0"
        return {
            "Status": estado,
            "ExtendedStatusForHumans": str(st.get("extended", "")).replace("\n", " · "),
            "StateOfCharge": str(st.get("state_of_charge", 0)),
            "Location": f"{p.get('x', 0)} {p.get('y', 0)} {p.get('th', 0)}",
            "LocalizationScore": score,
            "Temperature": str(st.get("temperature", "")),
        }
