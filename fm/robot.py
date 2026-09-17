"""Estado en runtime de un robot: cliente REST, índices nombre → GUID y la
última telemetría. Un objeto por robot, vivo durante toda la ejecución del FM.
"""
from __future__ import annotations

import logging

from fm.adapters.base import Telemetry
from fm.adapters.mir import MissionRequest, from_vda_order, to_telemetry
from fm.assigner import RobotSnapshot
from fm.config import FleetConfig, RobotConfig
from fm.mir_client import MirClient, MirStatus
from fm.orders import OrderTracker, pick_action
from fm.vda5050.order import Order, OrderRejected
from fm.vda5050.state import E_MOBILE_ROBOT_NOT_AVAILABLE

log = logging.getLogger("fm.robot")


class Robot:
    def __init__(self, cfg: RobotConfig, client: MirClient | None = None):
        self.cfg = cfg
        self.serial = cfg.serial
        self.client = client or MirClient(cfg.host, cfg.auth)
        # Prefijo [serial] en cada línea para poder filtrar el log por robot.
        self.log = logging.LoggerAdapter(log, {}) 
        self.log.process = lambda msg, kw: (f"[{self.serial}] {msg}", kw)
        # Índices por robot (los GUIDs no coinciden entre robots, §9.2).
        self.missions: dict[str, str] = {}      # nombre → GUID
        self.positions: dict[str, str] = {}     # nombre → GUID
        self.mission_inputs: dict[str, set[str]] = {}   # GUID mission → input_names
        self.indexed = False
        self.last_status: MirStatus | None = None
        self.last_telemetry: Telemetry | None = None
        self.last_error: str | None = None
        self.orders = OrderTracker(self.serial)

    def refresh_indices(self, charge_mission: str | None = None,
                        mission_group: str | None = None) -> bool:
        """Resuelve nombres → GUID. Tolerante: si falla, se reintenta en el
        próximo tick y mientras tanto el robot solo publica telemetría."""
        try:
            self.missions = self.client.index_missions_by_name(mission_group)
            st = self.last_status or self.client.status_get()
            self.positions = self.client.index_positions_by_name(st.map_id)
            wanted = {a.mission for a in self.cfg.actions.values()}
            if charge_mission:
                wanted.add(charge_mission)
            for name in sorted(wanted):
                guid = self.missions.get(name)
                if guid is None:
                    self.log.warning("mission '%s' no existe en este robot (irá a NO_ROUTE_TO_TARGET)", name)
                    continue
                self.mission_inputs[guid] = self.client.index_mission_params(guid)
            self.indexed = True
            self.log.info("índices: %d missions, %d positions", len(self.missions), len(self.positions))
            return True
        except Exception as e:   # red, 4xx, JSON raro: todo se reintenta luego
            self.log.warning("no se pudieron cargar los índices: %s", e)
            return False

    def poll(self) -> Telemetry | None:
        """GET /status tolerante → `Telemetry`. None si falla (y guarda el motivo)."""
        try:
            self.last_status = self.client.status_get()
            self.last_error = None
        except Exception as e:
            # Resumen corto: el detalle completo de requests es ilegible en state.errors
            self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
            self.log.warning("GET /status falló: %s", self.last_error)
            self.last_telemetry = None
            return None
        own = self.orders.active.queue_id if self.orders.busy else None
        self.last_telemetry = to_telemetry(self.last_status, own)
        return self.last_telemetry

    # ---------------------------------------------------------------- orders
    @property
    def busy(self) -> bool:
        """Ocupado = job del FM vivo, o el robot ejecuta algo que no es nuestro
        (lanzado desde la web) — decisión 16. Lo segundo lo decide el driver."""
        return self.orders.busy or self.foreign_mission

    @property
    def foreign_mission(self) -> bool:
        t = self.last_telemetry
        return t is not None and t.foreign_busy

    @property
    def available(self) -> bool:
        t = self.last_telemetry
        return t is not None and t.available

    def snapshot(self, charging: bool = False) -> RobotSnapshot:
        t = self.last_telemetry
        return RobotSnapshot(self.serial, t.battery if t else 0.0, self.busy,
                             self.available, charging, t.pose[:2] if t and t.pose else None)

    def translate_order(self, order: Order, cfg: FleetConfig) -> MissionRequest:
        """Valida reglas de ciclo de vida + traduce a mission. Sin red.
        Lanza IgnoreOrder / OrderRejected."""
        self.orders.check_new(order, self.last_telemetry)
        if self.foreign_mission:
            raise OrderRejected(E_MOBILE_ROBOT_NOT_AVAILABLE,
                                "el robot ejecuta una mission ajena al FM (lanzada desde la web)")
        if not self.indexed:
            raise OrderRejected(E_MOBILE_ROBOT_NOT_AVAILABLE, "índices de missions/positions no cargados")
        action = pick_action(order, self.orders.done_action_ids(order))
        return from_vda_order(action, self.cfg, self.missions, self.positions,
                              self.mission_inputs, cfg.positions_allowlist)

    def execute(self, order: Order, req: MissionRequest, priority: int = 0) -> int:
        """POST /mission_queue y registro en el tracker. Devuelve el queue id."""
        q = self.client.mission_queue_post(req.mission_guid, req.parameters or None, priority)
        self.orders.accept(order, req, int(q["id"]))
        return int(q["id"])
