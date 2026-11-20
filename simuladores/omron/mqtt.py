"""API MQTT del OMRON digital: lo que publica la aplicación «MQTT API» del LD.

En el robot real hay un broker propio (puerto 8883, TLS). Aquí se publica en
un broker cualquiera (el Mosquitto local, sin TLS), con los mismos topics y
payloads: el driver del FM se apunta a él con `mqtt_host`, `mqtt_port` y
`mqtt_tls: false` (decisión 207). Solo la telemetría; los comandos por MQTT
(`itk/cmd/arcl`, trabajos…) no se simulan, porque el FM manda por ARCL.

Ojo: los topics `itk/…` no llevan el nombre del robot, así que en un mismo
broker solo cabe un OMRON digital con MQTT.

Lo visto en el LD real (2026-11-20) y lo supuesto, marcado como en robot.py:
- visto: `status` ~5 veces/s, retenido, aunque nada cambie; fuera del dock
  su `status` es el `Status:` de ARCL y en el dock el texto para humanos.
- visto: `pose` en mm y grados enteros, retenido, solo al cambiar.
- visto: `goals` retenido; DataStore bajo petición, con su intervalo, y la
  respuesta «Forwarded» en `itk/cmd/dataStore/subscribe/res/<n>`.
- visto: `DockingState` en mayúsculas (`DOCKED`, `UNDOCKED`),
  `RobotChargeState` como el `ChargeState` de ARCL (`Bulk`, `Not`),
  `LaserLocalizationScore` de 0 a 100.
- supuesto: `mode` fuera de los casos vistos («Goal seeking», «Dock»,
  «EStop pressed»), el `qmState` salvo `Available/Available`,
  `Available/Docked` y `UnavailableNeedsAssistance/EStopPressed`, y qué
  contesta un nombre del DataStore que no existe.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

log = logging.getLogger("omron_digital")

Publicar = Callable[[str, str, bool], None]      # (topic, payload, retenido)


def _intervalo(texto: str) -> float:
    """"1s", "500ms", "2" → segundos."""
    t = texto.strip().lower()
    if t.endswith("ms"):
        return float(t[:-2]) / 1000
    return float(t.rstrip("s"))


class ApiMqtt:
    """Calcula lo que publicaría el robot. `tick(ahora)` se llama desde el
    hilo de MQTT con el candado del servidor ARCL cogido (lee el robot)."""

    PERIODO_STATUS = 0.2                          # visto: ~5 por segundo

    def __init__(self, robot, publicar: Publicar):
        self.robot = robot
        self.publicar = publicar
        self._ultimo_status = 0.0
        self._pose: tuple | None = None
        self._goals: list[str] | None = None
        self._qm: tuple | None = None
        self.datastore: dict[str, list[float]] = {}   # nombre → [intervalo, próximo envío]
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"{self._n:020x}"[:22]

    def _msg(self, tipo: str, **campos) -> str:
        return json.dumps({"type": tipo, "id": self._id(), "upd": int(time.time() * 1000), **campos})

    # --- valores ---

    def valor_datastore(self, nombre: str) -> str | None:
        r = self.robot
        return {
            "DockingState": r.docking.upper(),
            "RobotChargeState": r.carga_estado,
            "LaserLocalizationScore": f"{r.score * 100:.0f}",
            "BatteryStateOfCharge": f"{r.soc:.2f}",
            "RobotState": r.estado_cola()[0],
            "Queue_Job_ID": (r.trabajo_en_curso.job_id if r.trabajo_en_curso else ""),
        }.get(nombre)

    def _status(self) -> dict:
        r = self.robot
        ext, st = r.texto_status()
        if r.estop:
            modo = "EStop pressed"
        else:
            modo = {"dock": "Dock", "goal": "Goal seeking"}.get(r.modo, "Stop")   # "Stop": supuesto
        return {"status": ext if r.docking != "Undocked" else st, "mode": modo, "extended": ext,
                "wait_state": "Not waiting", "state_of_charge": round(r.soc, 1), "temperature": 39}

    def _qm_state(self) -> tuple[str, str]:
        r = self.robot
        if r.estop:
            return "UnavailableNeedsAssistance", "EStopPressed"
        estado, sub = r.estado_cola()
        if estado == "Available":
            return ("Available", "Docked") if r.docking == "Docked" else ("Available", "Available")
        return "Unavailable", sub

    # --- peticiones ---

    def pedir_datastore(self, nombre: str, payload: str) -> None:
        """Lo que hace el robot con `itk/cmd/dataStore/subscribe/req/<nombre>`."""
        try:
            intervalo = _intervalo(json.loads(payload)["interval"])
        except (ValueError, KeyError, TypeError):
            self.publicar("itk/cmd/dataStore/subscribe/res/error",
                          self._msg("SubscriptionConfig", status="Error", description="invalid payload"), False)
            return
        if self.valor_datastore(nombre) is None:
            self.publicar("itk/cmd/dataStore/subscribe/res/error",
                          self._msg("SubscriptionConfig", status="Error",
                                    description=f"Unknown dataStore {nombre}"), False)
            return
        self.datastore[nombre] = [intervalo, 0.0]
        self.publicar(f"itk/cmd/dataStore/subscribe/res/{nombre}",
                      json.dumps({"type": "SubscriptionConfig", "id": nombre, "upd": int(time.time() * 1000),
                                  "status": "Forwarded", "interval": f"{intervalo:g}s",
                                  "description": f"Forwarded subscription request for {nombre} "
                                                 f"with interval {intervalo:g}s to ARAM server."}), False)

    # --- reloj ---

    def tick(self, ahora: float) -> None:
        r = self.robot
        goals = list(r.mapa.goals)
        if goals != self._goals:
            self._goals = goals
            self.publicar("itk/dt/goals", self._msg("Goals", goals=goals), True)
        pose = (round(r.x), round(r.y), round(r.th))
        if pose != self._pose:
            self._pose = pose
            self.publicar("itk/dt/robot/pose", self._msg("RobotPosition", x=pose[0], y=pose[1], th=pose[2]), True)
        qm = self._qm_state()
        if qm != self._qm:
            self._qm = qm
            self.publicar("itk/dt/robot/qmState", self._msg("RobotQMState", state=qm[0], sub_state=qm[1]), True)
        if ahora - self._ultimo_status >= self.PERIODO_STATUS:
            self._ultimo_status = ahora
            self.publicar("itk/dt/robot/status", self._msg("RobotStatus", **self._status()), True)
        for nombre, prog in self.datastore.items():
            if ahora >= prog[1]:
                prog[1] = ahora + prog[0]
                self.publicar(f"itk/dt/dataStore/value/{nombre}",
                              json.dumps({"type": "DataStoreValue", "id": nombre,
                                          "upd": int(time.time() * 1000), "value": self.valor_datastore(nombre)}),
                              False)


class PublicadorMQTT:
    """Conecta `ApiMqtt` a un broker de verdad, en su propio hilo."""

    def __init__(self, servidor, host: str = "localhost", port: int = 1883, periodo: float = 0.05):
        import paho.mqtt.client as mqtt
        self.servidor = servidor                 # ServidorARCL: su robot y su candado
        self.host, self.port, self.periodo = host, port, periodo
        self._c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"omron-digital-{int(time.time())}")
        self.api = ApiMqtt(servidor.robot, lambda t, p, r: self._c.publish(t, p, qos=0, retain=r))
        self._c.on_connect = lambda c, u, f, rc, p=None: c.subscribe("itk/cmd/dataStore/subscribe/req/+", qos=1)
        self._c.on_message = self._al_recibir
        self._parar = threading.Event()

    def _al_recibir(self, c, u, m) -> None:
        with self.servidor.candado:
            self.api.pedir_datastore(m.topic.rsplit("/", 1)[1], m.payload.decode("utf-8", "replace"))

    def arrancar(self) -> None:
        self._c.connect_async(self.host, self.port, keepalive=60)
        self._c.loop_start()
        threading.Thread(target=self._bucle, name="omron-mqtt", daemon=True).start()
        log.info("API MQTT simulada en %s:%d (topics itk/…)", self.host, self.port)

    def parar(self) -> None:
        self._parar.set()
        self._c.loop_stop()
        self._c.disconnect()

    def _bucle(self) -> None:
        while not self._parar.wait(self.periodo):
            if self._c.is_connected():
                with self.servidor.candado:
                    self.api.tick(time.monotonic())
