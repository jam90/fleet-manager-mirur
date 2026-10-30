"""`MirDriver` contra un cliente falso: connect → poll → translate → execute → job_status."""
import pytest

from fm.adapters.mir import MirDriver
from fm.adapters.mir.client import MirClient
from fm.adapters.mir.config import ActionConfig, MirRobotConfig
from fm.vda5050.order import Action, ActionParameter, OrderRejected

CFG = MirRobotConfig("mir-1", "h", "a", {
    "coger": ActionConfig("coger", "coger"),
    "ir_a": ActionConfig("ir_a", "Ir a posición", position_inputs=["target_pos"]),
}, mission_group="g", charge_mission="Carga")
STATUS = {"state_id": 3, "state_text": "Ready", "battery_percentage": 70.0, "map_id": "m",
          "position": {"x": 0, "y": 0, "orientation": 0}, "mission_queue_id": None}


class _FakeClient(MirClient):
    def __init__(self, fail_status=False):
        super().__init__("fake", "Basic x")
        self.fail_status = fail_status
        self.status = dict(STATUS)
        self.queue: dict[int, str] = {}
        self.messages: dict[int, str] = {}       # mission_queue/<id>.message
        self.posted: list[dict] = []
        self.deleted: list[int] = []
        self.site_reads = 0
        self.zone_reads = 0
        self.extra_positions: list[dict] = []
        self.actions: dict[str, list[dict]] = {}     # missions/<guid>/actions a medida

    def _req(self, method, path, json=None):
        if method == "GET" and path == "status":
            if self.fail_status:
                raise ConnectionError("sin red")
            return self.status
        if path == "mission_groups":
            return [{"name": "g", "guid": "g-guid"}]
        if path == "mission_groups/g-guid/missions":
            return [{"name": "coger", "guid": "m-coger"}, {"name": "Ir a posición", "guid": "m-ir"},
                    {"name": "Carga", "guid": "m-carga"}]
        if path == "positions":
            return [{"name": "P1", "guid": "p-1", "map": "/v2.0.0/maps/m"}, *self.extra_positions]
        if path == "maps":
            return [{"guid": "m", "name": "taller"}, {"guid": "m-otro", "name": "almacén"}]
        if path.startswith("missions/") and path.endswith("/actions"):
            if path in self.actions:
                return self.actions[path]
            return [{"parameters": [{"input_name": "target_pos"}]}] if "m-ir" in path else []
        if method == "POST" and path == "mission_queue":
            self.posted.append(json)
            qid = 100 + len(self.posted)
            self.queue[qid] = "Pending"
            return {"id": qid}
        if method == "GET" and path.startswith("mission_queue/"):
            qid = int(path.split("/")[1])
            return {"state": self.queue[qid], "message": self.messages.get(qid, "")}
        if method == "DELETE" and path.startswith("mission_queue/"):
            self.deleted.append(int(path.split("/")[1]))
            return None
        if method == "GET" and path == "maps/m":            # site del mapa activo (decisión 129)
            self.site_reads += 1
            return {"guid": "m", "session_id": "s-1"}
        if method == "GET" and path == "sessions/s-1":
            return {"guid": "s-1", "name": "planta"}
        if method == "GET" and path == "maps/m/zones":     # zona de espera (decisión 138)
            self.zone_reads += 1
            return [{"guid": "z-1", "name": "zona de espera"}, {"guid": "z-2", "name": "otra"}]
        if method == "GET" and path == "zones/z-1":
            return {"guid": "z-1", "polygon": [{"x": 34.4, "y": 7.2}, {"x": 36.2, "y": 7.2},
                                               {"x": 36.2, "y": 9.8}, {"x": 34.4, "y": 9.8}]}
        if method == "PUT" and path == "status":
            self.status["state_id"] = json["state_id"]
            return self.status
        raise AssertionError(f"llamada inesperada {method} {path}")


def _driver(**kw):
    c = _FakeClient(**kw)
    return MirDriver(CFG, c), c


def test_connect_indices_y_translate():
    d, _ = _driver()
    assert d.manufacturer == "MiR"
    assert d.connect() and d.indexed
    assert d.mission_names == {"m-coger": "coger", "m-ir": "Ir a posición", "m-carga": "Carga"}
    # Una mission se puede nombrar por su nombre o por su GUID (decisión 87).
    assert d.missions == {"coger": "m-coger", "Ir a posición": "m-ir", "Carga": "m-carga",
                          "m-coger": "m-coger", "m-ir": "m-ir", "m-carga": "m-carga"}
    assert d.positions == {"P1": "p-1"}
    job = d.translate(Action("ir_a", "a1", actionParameters=[ActionParameter("target_pos", "P1")]))
    assert job.label == "mission 'Ir a posición'"
    assert job.payload.parameters == [{"id": "target_pos", "value": "p-1"}]
    with pytest.raises(OrderRejected) as e:
        d.translate(Action("volar", "a2"))
    assert e.value.error_type == "INVALID_ORDER_ACTION"


def test_sin_indices_rechaza_y_poll_tolerante():
    d, c = _driver(fail_status=True)
    assert not d.connect() and not d.indexed
    assert d.poll() is None and "ConnectionError" in d.last_error
    with pytest.raises(OrderRejected) as e:
        d.translate(Action("coger", "a1"))
    assert e.value.error_type == "MOBILE_ROBOT_NOT_AVAILABLE"
    c.fail_status = False
    t = d.poll()
    assert t is not None and t.battery == 70.0 and d.last_error is None


def test_execute_job_status_y_propia_vs_ajena():
    d, c = _driver()
    d.connect()
    job = d.translate(Action("coger", "a1"))
    jid = d.execute(job, priority=5)
    assert jid == "101" and c.posted == [{"mission_id": "m-coger", "priority": 5}]
    assert d.job_status(jid) == "WAITING"
    # El robot ejecuta NUESTRA mission → no es ajena
    c.queue[101] = "Executing"
    c.status.update(state_id=5, mission_queue_id=101)
    assert d.job_status(jid) == "RUNNING"
    assert not d.poll().foreign_busy
    # Termina; luego alguien lanza otra desde la web → ajena
    c.queue[101] = "Done"
    assert d.job_status(jid) == "FINISHED"
    c.status.update(mission_queue_id=555)
    assert d.poll().foreign_busy
    d.cancel(jid)
    assert c.deleted == [101]
    d.pause()
    assert c.status["state_id"] == 4 and d.poll().paused
    d.resume()
    assert c.status["state_id"] == 3


def test_job_status_none_si_falla_y_estado_raro():
    d, c = _driver()
    assert d.job_status("999") is None          # KeyError en el fake = fallo de red
    c.queue[7] = "Marciano"
    assert d.job_status("7") is None


def test_charge_job():
    d, _ = _driver()
    assert d.charge_job() is None               # sin índices aún
    d.connect()
    job = d.charge_job()
    assert job.payload.mission_guid == "m-carga" and job.action.actionType == "charge"
    d2 = MirDriver(MirRobotConfig("mir-1", "h", "a", {}, mission_group="g"), _FakeClient())
    d2.connect()
    assert d2.charge_job() is None              # sin mission de carga configurada


def test_number_input_llega_como_numero_al_post(caplog):
    """`n_piezas_pedidas` (registro PLC 22): la UI manda "5", el MiR recibe 5,
    con el input_name como `id` (no el GUID del parámetro, distinto por robot)."""
    cfg = MirRobotConfig("mir-1", "h", "a", {
        "pedir": ActionConfig("pedir", "Ir a posición", number_inputs=["n_piezas_pedidas"]),
    }, mission_group="g")
    c = _FakeClient()
    d = MirDriver(cfg, c)
    with caplog.at_level("WARNING", logger="fm.mir"):
        assert d.connect()
    # La mission falsa solo expone target_pos: aviso al arrancar, no error.
    assert "no tiene el input 'n_piezas_pedidas'" in caplog.text
    job = d.translate(Action("pedir", "a1", actionParameters=[ActionParameter("n_piezas_pedidas", "5")]))
    d.execute(job)
    assert c.posted[-1]["parameters"] == [{"id": "n_piezas_pedidas", "value": 5}]
    assert [(p.key, p.kind) for p in d.describe_actions()[0].params] == [("n_piezas_pedidas", "number")]


def test_describe_actions_allowlist_de_action_y_rango(caplog):
    """La UI solo ofrece las positions de la allowlist de la action que existen
    en el robot, y recibe el rango del número para pintar el deslizador."""
    from fm.adapters.mir.config import NumberRange
    cfg = MirRobotConfig("mir-1", "h", "a", {
        "llevar": ActionConfig("llevar", "Ir a posición", position_inputs=["target_pos"],
                               positions_allowlist={"P1", "NoExiste"}, number_inputs=["n"],
                               number_ranges={"n": NumberRange(1, 10, 1)}),
        "ir_a": ActionConfig("ir_a", "Ir a posición", position_inputs=["target_pos"]),
    }, mission_group="g")
    d = MirDriver(cfg, _FakeClient())
    # Antes de conectar: la allowlist tal cual (es config, no hace falta red).
    assert d.describe_actions()[0].params[0].choices == ["NoExiste", "P1"]
    with caplog.at_level("WARNING", logger="fm.mir"):
        assert d.connect()
    assert "la position 'NoExiste' de la allowlist no existe" in caplog.text
    llevar, ir_a = d.describe_actions()
    pos, n = llevar.params
    assert pos.choices == ["P1"]
    assert (n.kind, n.min, n.max, n.step) == ("number", 1, 10, 1)
    assert ir_a.params[0].choices == ["P1"]          # sin allowlist: todas las del robot


def test_job_result_es_el_message_de_la_cola():
    d, c = _driver()
    d.connect()
    qid = d.execute(d.translate(Action("coger", "a1")))
    c.queue[int(qid)], c.messages[int(qid)] = "Aborted", "Aborted - User Request"
    assert d.job_status(qid) == "FAILED"
    assert d.job_result(qid) == "Aborted - User Request"
    assert d.job_result(qid) is None          # se entrega una vez


def test_guid_de_la_flota_no_lo_tapa_una_copia_con_el_mismo_nombre():
    # Visto en mir-1 el 2026-10-23 (decisión 118): un «Guardar como» dejó dos
    # «coger»; la copia salía primero y el FM no encontraba la de la flota.
    flota = "imperialfleet-mission000-01-0001-001"
    c = _FakeClient()
    orig = c._req

    def req(method, path, json=None):
        if path == "mission_groups/g-guid/missions":
            return [{"name": "coger", "guid": "181aad0a-copia"}, {"name": "coger", "guid": flota}]
        return orig(method, path, json)
    c._req = req
    cfg = MirRobotConfig("mir-1", "h", "a", {"coger": ActionConfig("coger", flota)}, mission_group="g")
    d = MirDriver(cfg, client=c)
    assert d.connect()
    assert d.missions[flota] == flota                 # por GUID, siempre
    assert d.missions["coger"] == "181aad0a-copia"    # por nombre, el primero (con aviso)
    assert d.mission_names[flota] == "coger"


def test_poll_dice_el_site_del_mapa_activo_y_lo_pregunta_una_vez():
    d, c = _driver()
    t = d.poll()
    assert (t.site_id, t.site_name) == ("s-1", "planta")
    d.poll()
    assert c.site_reads == 1                        # cacheado por mapa
    c.status["map_id"] = "otro"                     # mapa que el robot no sabe describir
    t = d.poll()
    assert t is not None and t.site_id is None      # no se sabe: no se compara


def test_mission_con_positions_fijas_solo_en_su_mapa():
    # Decisión 131: `coger` va a una position fija del mapa «almacén»; con
    # otro mapa activo se rechaza (y la flota prueba otro robot). Un input
    # (target_pos) no ata a ningún mapa: se resuelve en el activo.
    d, c = _driver()
    c.extra_positions = [{"name": "Muelle", "guid": "p-muelle", "map": "/v2.0.0/maps/m-otro"}]
    c.actions["missions/m-coger/actions"] = [
        {"parameters": [{"id": "position", "value": "p-muelle", "input_name": None}]}]
    c.actions["missions/m-ir/actions"] = [
        {"parameters": [{"id": "position", "value": "p-muelle", "input_name": "target_pos"}]}]
    d.poll()
    assert d.connect()
    assert d.mission_maps == {"m-coger": {"m-otro"}, "m-ir": set(), "m-carga": set()}
    with pytest.raises(OrderRejected, match="mapa «almacén» y el robot está en «taller»") as e:
        d.translate(Action("coger", "a1"))
    assert e.value.error_type == "NO_ROUTE_TO_TARGET"
    d.translate(Action("ir_a", "a2", actionParameters=[ActionParameter("target_pos", "P1")]))
    c.status["map_id"] = "m-otro"
    d.poll()
    assert d.translate(Action("coger", "a3")).label == "mission 'coger'"


def test_las_positions_fijas_de_una_sub_mission_tambien_atan():
    d, c = _driver()
    c.extra_positions = [{"name": "Muelle", "guid": "p-muelle", "map": "/v2.0.0/maps/m-otro"}]
    c.actions["missions/m-coger/actions"] = [
        {"parameters": [{"id": "mission_id", "value": "m-sub", "input_name": None}]}]
    c.actions["missions/m-sub/actions"] = [
        {"parameters": [{"id": "position", "value": "p-muelle", "input_name": None}]},
        {"parameters": [{"id": "mission_id", "value": "m-coger", "input_name": None}]}]   # ciclo
    d.poll()
    assert d.connect() and d.mission_maps["m-coger"] == {"m-otro"}


def test_point_in_polygon():
    from fm.adapters.mir.client import point_in_polygon
    ele = [{"x": 0, "y": 0}, {"x": 4, "y": 0}, {"x": 4, "y": 1}, {"x": 1, "y": 1}, {"x": 1, "y": 4}, {"x": 0, "y": 4}]
    assert point_in_polygon(0.5, 3, ele) and point_in_polygon(3, 0.5, ele)
    assert not point_in_polygon(3, 3, ele) and not point_in_polygon(-1, 0.5, ele)   # el hueco de la L y fuera


def test_in_zone_por_guid_o_nombre_y_lo_guarda_un_rato():
    from fm.adapters.base import Telemetry
    t = {"now": 0.0}
    c = _FakeClient()
    d = MirDriver(CFG, c, clock=lambda: t["now"])
    dentro = Telemetry(70.0, pose=(35.2, 9.0, 0.0), map_id="m")      # Punto espera 1
    fuera = Telemetry(70.0, pose=(31.4, 7.8, 0.0), map_id="m")
    assert d.in_zone("z-1", dentro) is True and d.in_zone("z-1", fuera) is False
    assert d.in_zone("zona de espera", dentro) is True
    assert c.zone_reads == 2                         # una por (mapa, zona), no una por tick
    t["now"] += 61
    d.in_zone("z-1", dentro)
    assert c.zone_reads == 3                         # pasado ZONE_TTL_S se relee
    # Sin saberlo: zona que no está en el mapa, otro mapa sin zonas, sin pose.
    assert d.in_zone("no-existe", dentro) is None
    assert d.in_zone("z-1", Telemetry(70.0, map_id="m")) is None
