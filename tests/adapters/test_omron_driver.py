"""Driver OMRON contra el OMRON digital (simuladores/omron) por un socket de
verdad: lo mismo que hará con el LD real, pero acelerado."""
from __future__ import annotations

import time

import pytest

from fm.adapters.omron import OmronDriver
from fm.adapters.omron.driver import OmronAction, OmronRobotConfig
from fm.vda5050.order import Action, ActionParameter, OrderRejected
from simuladores.omron import OmronDigital, ServidorARCL, leer_mapa

MAPA = """2D-Map-Ex4
Cairn: GoalWithHeading 1000 0 0.0 "" ICON "Taller 2"
Cairn: GoalWithHeading 2000 0 0.0 "" ICON "Almacen"
RouteInfo: Macro "Macro1"
RouteInfo: Task "wait" 1
RouteInfo: MacroTemplate "MacroTemplateFM"
RouteInfo: GoalTask "goto" "$1"
RouteInfo: _goto "$1"
RouteInfo: Task "wait" "$2"
RouteInfo: MacroTemplate "MacroTemplateCoordenadas"
RouteInfo: Task "gotoPoint" "$1" "$2" "$3"
LINES
DATA
"""

ACCIONES = {
    "ir_y_esperar": OmronAction("ir_y_esperar", "template", nombre="MacroTemplateFM",
                                params=["destino", "espera_s"], goal_params=["destino"],
                                number_params=["espera_s"]),
    "ir_a": OmronAction("ir_a", "goal", goal_input="destino"),
    "saludar": OmronAction("saludar", "macro", nombre="Macro1"),
    # «Ir a punto» (decisión 206): x e y en mm y el ángulo en grados, enteros
    "ir_a_punto": OmronAction("ir_a_punto", "template", nombre="MacroTemplateCoordenadas",
                              params=["x_mm", "y_mm", "theta_deg"], number_params=["x_mm", "y_mm", "theta_deg"]),
}


@pytest.fixture
def entorno(tmp_path):
    f = tmp_path / "prueba.map"
    f.write_text(MAPA, encoding="latin-1")
    robot = OmronDigital(mapa=leer_mapa(f), password="clave", dock=(0, 0, 0))
    srv = ServidorARCL(("127.0.0.1", 0), robot, periodo=0.02, acelerar=25)
    srv.arrancar()
    d = OmronDriver(OmronRobotConfig("omron-1", "127.0.0.1", "clave", srv.puerto, ACCIONES))
    yield d, robot, srv
    d.client.cerrar()
    srv.parar()


def esperar(d: OmronDriver, job_id: str, hasta=("FINISHED", "FAILED"), max_s: float = 10.0) -> str:
    fin = time.monotonic() + max_s
    while time.monotonic() < fin:
        d.poll()
        if (e := d.job_status(job_id)) in hasta:
            return e
        time.sleep(0.05)
    return d.job_status(job_id)


def accion(tipo, **params):
    return Action(tipo, "a1", actionParameters=[ActionParameter(k, v) for k, v in params.items()])


def test_conecta_y_lee_el_robot(entorno):
    d, robot, _ = entorno
    assert d.connect() is True
    assert d.goals == ["Taller 2", "Almacen"] and d.macros == ["Macro1"] and d.mapa == "prueba.map"
    t = d.poll()
    assert t.battery == pytest.approx(99.6, abs=0.1) and t.charging is True and t.available
    assert t.map_id == "prueba.map" and t.pose == (0.0, 0.0, 0.0)
    assert "activado" in d.autodock()


def test_template_con_parametros_hasta_terminar(entorno):
    d, robot, _ = entorno
    d.connect()
    job = d.translate(accion("ir_y_esperar", destino="Taller 2", espera_s="2"))
    assert job.payload["valores"] == ['"Taller 2"', "2"]
    job_id = d.execute(job)
    assert d.job_status(job_id) == "RUNNING"
    assert esperar(d, job_id) == "FINISHED"
    assert (round(robot.x), round(robot.y)) == (1000, 0)


def test_goal_inexistente_se_rechaza_antes_de_mandar(entorno):
    """En el LD real, el template con un goal que no existe 'termina' sin error."""
    d, _, _ = entorno
    d.connect()
    with pytest.raises(OrderRejected) as e:
        d.translate(accion("ir_y_esperar", destino="NoExiste", espera_s=1))
    assert e.value.error_type == "NO_ROUTE_TO_TARGET"
    with pytest.raises(OrderRejected) as e:
        d.translate(accion("ir_y_esperar", destino="Almacen", espera_s="mucho"))
    assert e.value.error_type == "VALIDATION_FAILURE"


def test_trabajo_de_la_cola_y_cancelar(entorno):
    d, robot, _ = entorno
    d.connect()
    job = d.translate(accion("ir_a", destino="Almacen"))
    job.tag = "fleet-1234"
    job_id = d.execute(job)
    assert job_id == "fleet_1234"
    assert esperar(d, job_id) == "FINISHED"
    job_id = d.execute(d.translate(accion("ir_a", destino="Taller 2")))
    assert esperar(d, job_id, hasta=("RUNNING",)) == "RUNNING"
    d.cancel(job_id)
    assert esperar(d, job_id) == "FAILED"


def test_macro_y_ocupado_por_otro(entorno):
    d, robot, srv = entorno
    d.connect()
    job_id = d.execute(d.translate(accion("saludar")))
    assert esperar(d, job_id) == "FINISHED"
    with srv.candado:
        robot.goto("Almacen")            # lo mueve otro (MobilePlanner, el PLC…)
    fin = time.monotonic() + 5
    while time.monotonic() < fin:
        t = d.poll()
        if t and t.foreign_busy:
            break
        time.sleep(0.05)
    assert t.foreign_busy and t.driving


def test_sin_robot_no_lanza(tmp_path):
    d = OmronDriver(OmronRobotConfig("omron-1", "127.0.0.1", "x", 1, ACCIONES))
    assert d.connect() is False and d.poll() is None and d.last_error


def test_ir_a_punto_con_coordenadas(entorno):
    """Decisión 206, ahora contra el digital: llega al punto y con ese rumbo."""
    d, robot, _ = entorno
    d.connect()
    job = d.translate(accion("ir_a_punto", x_mm="1500", y_mm=-500, theta_deg="-89"))
    assert job.payload["valores"] == ["1500", "-500", "-89"]
    assert esperar(d, d.execute(job), max_s=15) == "FINISHED"
    assert (round(robot.x), round(robot.y), round(robot.th)) == (1500, -500, -89)


# --- retomar tras reiniciar el FM (decisión 214) ---

def _otro_fm(srv) -> OmronDriver:
    """El driver de un FM recién arrancado: no sabe nada de lo que lanzó el anterior."""
    d = OmronDriver(OmronRobotConfig("omron-1", "127.0.0.1", "clave", srv.puerto, ACCIONES))
    assert d.connect()
    return d


def test_retomar_un_trabajo_de_la_cola(entorno):
    d, robot, srv = entorno
    d.connect()
    job = d.translate(accion("ir_a", destino="Almacen"))
    job.tag = "fleet-77@1764060000.123"
    job_id = d.execute(job)
    nuevo = _otro_fm(srv)
    try:
        assert nuevo.find_job(job.tag) == job_id
        nuevo.adopt_job(job_id)
        assert nuevo.job_status(job_id) in ("WAITING", "RUNNING")
        assert esperar(nuevo, job_id) == "FINISHED"
        # Uno ya terminado también se retoma, y sale ya terminado.
        otro = _otro_fm(srv)
        otro.adopt_job(job_id)
        assert otro.job_status(job_id) == "FINISHED"
        otro.client.cerrar()
    finally:
        nuevo.client.cerrar()


def test_si_no_esta_en_la_cola_no_se_sabe(entorno):
    """No se sabe si el LD guarda los terminados: no se relanza, queda en duda."""
    _, _, srv = entorno
    nuevo = _otro_fm(srv)
    try:
        with pytest.raises(LookupError, match="no se sabe"):
            nuevo.find_job("fleet-nunca@1.0")
    finally:
        nuevo.client.cerrar()


def test_una_macro_no_se_puede_retomar(entorno):
    d, _, srv = entorno
    d.connect()
    job_id = d.execute(d.translate(accion("saludar")))
    assert job_id.startswith("macro-")
    nuevo = _otro_fm(srv)
    try:
        with pytest.raises(ValueError, match="no tienen id"):
            nuevo.adopt_job(job_id)
    finally:
        nuevo.client.cerrar()
