"""Cliente REST del MiR250 (API v2.0.0).

Nombres de métodos calcados del path (`<recurso>_<verbo>`) para poder grep-ear
el endpoint. No hay lógica de negocio aquí: solo HTTP + los índices
nombre → GUID que el resto del FM necesita (los GUIDs son distintos en cada
robot aunque el nombre coincida, así que se construye un índice por robot).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger("fm.mir")

# Estados de `/status.state_id` que nos interesan (MiR REST API 2.x).
STATE_STARTING = 1
STATE_SHUTTING_DOWN = 2
STATE_READY = 3
STATE_PAUSE = 4
STATE_EXECUTING = 5
STATE_ABORTED = 6
STATE_COMPLETED = 7
STATE_EMERGENCY_STOP = 10
STATE_MANUAL = 11
STATE_ERROR = 12

# Estados de una entrada de `/mission_queue/<id>`.
QUEUE_ALIVE = {"Pending", "Executing"}
QUEUE_FINISHED = {"Done", "Aborted", "Cancelled", "Canceled"}


def position_map_id(p: dict) -> str:
    """GUID del mapa de una position del listado `/positions` (campo `map` URL)."""
    if p.get("map_id"):
        return str(p["map_id"])
    return str(p.get("map", "")).rsplit("/", 1)[-1]


class MirApiError(RuntimeError):
    """Error HTTP del MiR con el motivo que devuelve en el body.

    `raise_for_status()` pierde el body y el MiR explica ahí el problema
    (`message`, `field_errors`), así que lo incorporamos al mensaje.
    """

    def __init__(self, method: str, path: str, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} → HTTP {status}: {body}")


@dataclass
class MirStatus:
    """Subconjunto de `GET /status` que usa el FM."""
    state_id: int
    state_text: str
    battery_percentage: float
    battery_time_remaining: int | None       # segundos; candidato a delatar la carga
    mode_id: int | None
    mode_text: str
    x: float
    y: float
    theta: float                             # radianes en [-π, π] (el MiR da grados)
    map_id: str
    mission_text: str
    mission_queue_id: int | None             # entrada de la cola en ejecución, si la hay
    errors: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: dict) -> "MirStatus":
        pos = d.get("position") or {}
        deg = float(pos.get("orientation", 0.0))
        # VDA quiere radianes en [-π, π]; atan2 normaliza el rango de golpe.
        theta = math.atan2(math.sin(math.radians(deg)), math.cos(math.radians(deg)))
        return cls(
            state_id=int(d.get("state_id", 0)),
            state_text=str(d.get("state_text", "")),
            battery_percentage=float(d.get("battery_percentage", 0.0)),
            battery_time_remaining=d.get("battery_time_remaining"),
            mode_id=d.get("mode_id"),
            mode_text=str(d.get("mode_text", "")),
            x=float(pos.get("x", 0.0)),
            y=float(pos.get("y", 0.0)),
            theta=theta,
            map_id=str(d.get("map_id", "")),
            mission_text=str(d.get("mission_text", "")),
            mission_queue_id=d.get("mission_queue_id"),
            errors=list(d.get("errors") or []),
            raw=d,
        )


class MirClient:
    def __init__(self, host: str, auth: str, timeout: float | tuple[float, float] = (2.0, 5.0)):
        self.host = host
        self.prefix = f"http://{host}/api/v2.0.0/"
        # (connect, read): un robot apagado no debe bloquear 5 s el tick de los
        # demás; 2 s de connect bastan en LAN.
        self.timeout = timeout
        self.headers = {
            "Content-Type": "application/json",
            "Accept-Language": "en_US",
            "Authorization": auth,
        }
        # Una Session reutiliza la conexión TCP entre ticks (1 Hz por robot).
        self._s = requests.Session()

    # ------------------------------------------------------------------ HTTP
    def _req(self, method: str, path: str, json: Any = None) -> Any:
        r = self._s.request(method, self.prefix + path, headers=self.headers,
                            json=json, timeout=self.timeout)
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = r.text
            raise MirApiError(method, path, r.status_code, body)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    # -------------------------------------------------------------- endpoints
    def status_get(self) -> MirStatus:
        return MirStatus.from_json(self._req("GET", "status"))

    def status_get_raw(self) -> dict:
        return self._req("GET", "status")

    def status_put(self, state_id: int) -> dict:
        """3 = Ready (reanudar), 4 = Pause."""
        return self._req("PUT", "status", {"state_id": state_id})

    def missions_get(self) -> list[dict]:
        return self._req("GET", "missions")

    def mission_groups_get(self) -> list[dict]:
        return self._req("GET", "mission_groups")

    def mission_groups_group_id_missions_get(self, group_guid: str) -> list[dict]:
        """Missions de un grupo. Es la única forma de filtrar por grupo:
        `/missions?group_id=` no filtra y `/missions` no trae `group_id` (§9.4)."""
        return self._req("GET", f"mission_groups/{group_guid}/missions")

    def missions_mission_id_actions_get(self, mission_guid: str) -> list[dict]:
        return self._req("GET", f"missions/{mission_guid}/actions")

    def positions_get(self) -> list[dict]:
        return self._req("GET", "positions")

    def mission_queue_post(self, mission_guid: str, parameters: list[dict] | None = None,
                           priority: int = 0) -> dict:
        """Encola una mission. `parameters` = [{"id": <input_name>, "value": ...}].

        `id` es el `input_name` de Blockly (lo que ve el usuario), NO el id
        interno de la action: con el interno el MiR responde 400
        `parameter_input_name_not_valid`.
        """
        body: dict[str, Any] = {"mission_id": mission_guid, "priority": priority}
        if parameters:
            body["parameters"] = parameters
        return self._req("POST", "mission_queue", body)

    def mission_queue_id_get(self, queue_id: int) -> dict:
        return self._req("GET", f"mission_queue/{queue_id}")

    def mission_queue_id_delete(self, queue_id: int) -> None:
        self._req("DELETE", f"mission_queue/{queue_id}")

    # ---------------------------------------------------------------- índices
    def index_mission_groups_by_name(self) -> dict[str, str]:
        return {g["name"]: g["guid"] for g in self.mission_groups_get()}

    def index_missions_by_name(self, group_name: str | None = None) -> dict[str, str]:
        """nombre → GUID. Con `group_name` solo se indexan las missions de ese
        grupo (menos ruido y menos peticiones); si el grupo no existe en el
        robot se lanza KeyError con un mensaje claro. Nombres duplicados: el primero."""
        if group_name is None:
            missions = self.missions_get()
        else:
            groups = self.index_mission_groups_by_name()
            if group_name not in groups:
                raise KeyError(f"[{self.host}] no existe el mission_group '{group_name}'; "
                               f"hay: {sorted(groups)}")
            missions = self.mission_groups_group_id_missions_get(groups[group_name])
        idx: dict[str, str] = {}
        for m in missions:
            name = m.get("name", "")
            if name in idx:
                log.warning("[%s] mission duplicada '%s': se usa %s, se ignora %s",
                            self.host, name, idx[name], m.get("guid"))
                continue
            idx[name] = m["guid"]
        return idx

    def index_positions_by_name(self, active_map_id: str | None = None) -> dict[str, str]:
        """nombre → GUID de positions (incluye markers de docking).

        Con nombres duplicados (p.ej. dos 'Charging station' con type_id
        distinto) se prefiere la del mapa activo; si persiste el empate, la
        primera + warning. Ver docs/avance.md, decisión 15.
        """
        by_name: dict[str, list[dict]] = {}
        for p in self.positions_get():
            # El listado no trae `map_id`, solo `map` = "/v2.0.0/maps/<guid>".
            p = {**p, "map_id": position_map_id(p)}
            by_name.setdefault(p.get("name", ""), []).append(p)
        idx: dict[str, str] = {}
        for name, cands in by_name.items():
            if len(cands) > 1 and active_map_id:
                in_map = [c for c in cands if c.get("map_id") == active_map_id]
                if in_map:
                    cands = in_map
            if len(cands) > 1:
                log.warning("[%s] position duplicada '%s' (type_ids %s): se usa la primera",
                            self.host, name, [c.get("type_id") for c in cands])
            idx[name] = cands[0]["guid"]
        return idx

    def index_mission_params(self, mission_guid: str) -> set[str]:
        """`input_name`s que expone la mission (para validar antes de postear)."""
        names: set[str] = set()
        for a in self.missions_mission_id_actions_get(mission_guid):
            for p in a.get("parameters") or []:
                if p.get("input_name"):
                    names.add(p["input_name"])
        return names
