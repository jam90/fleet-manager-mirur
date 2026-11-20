"""OMRON digital (simuladores/omron): que conteste como el LD real y que se
comporte como esperamos que lo haga el robot.

Los textos de `test_igual_que_el_LD_*` están copiados de la sesión ARCL con
el LD real del 2026-11-20: si cambian, es que el digital se ha separado del
robot.
"""
from __future__ import annotations

import socket
from datetime import datetime

import pytest

from simuladores.omron import OmronDigital, ServidorARCL, leer_mapa
from simuladores.omron.arcl import Sesion
from simuladores.omron.robot import DATOS, poner_config

MAPA = DATOS / "aula_ejemplo.map"
FECHA = datetime(2026, 11, 20, 9, 3, 4)


def robot(**kw) -> OmronDigital:
    kw.setdefault("reloj", lambda: FECHA)
    return OmronDigital(mapa=leer_mapa(MAPA), **kw)


def correr(r: OmronDigital, segundos: float, dt: float = 0.5) -> list[str]:
    """Avanza la simulación y devuelve los eventos que habría repartido."""
    eventos = []
    for _ in range(int(segundos / dt)):
        r.avanzar(dt)
        eventos += r.eventos
        r.eventos = []
    return eventos


# --- mapa ---------------------------------------------------------------------

def test_mapa_de_ejemplo():
    m = leer_mapa(MAPA)
    assert list(m.goals) == ["habitacion1", "habitacion2", "descanso"]   # orden del fichero
    assert m.rutas == {"Ruta1": ["descanso", "habitacion1", "habitacion2"]}
    g = m.goals["habitacion1"]
    assert (g.x, g.y, g.th) == (-8016, 6148, 0.0)
    prohibida = m.objeto("Forbidden Line1")
    assert prohibida.tipo == "ForbiddenLine" and prohibida.esquinas == (-7221, 1287, -8644, 4406)
    assert len(m.lineas) == 39


# --- igual que el LD real -------------------------------------------------------

def test_igual_que_el_LD_status_en_el_dock_sin_localizar():
    s = Sesion(robot(localizado=False, soc=99.5))
    assert s.responder("status\r\n") == [
        "ExtendedStatusForHumans: Charging, connected to Charging Station ",
        "Status: DockingState: Docked ForcedState: Unforced ChargeState: Float",
        "StateOfCharge: 99.5",
        "Location: -6415 566 -80",
        "LocalizationScore: 0.000000",
        "Temperature: 39",
    ]
    assert s.responder("oneLineStatus") == [
        "Status: DockingState: Docked ForcedState: Unforced ChargeState: Float "
        "StateOfCharge: 99.5 Location: -6415 566 -80 Temperature: 39"]
    assert s.responder("queueShow") == ['QueueRobot: "Omron-LD" UnAvailable Lost ""', "EndQueueShow"]
    assert s.responder("modeQuery") == ['ModeQuery: dock "Dock" true true false']


def test_igual_que_el_LD_mapa():
    s = Sesion(robot())
    assert s.responder("getgoals") == ["Goal: habitacion1", "Goal: habitacion2", "Goal: descanso",
                                       "End of goals"]
    assert s.responder("getRoutes") == ["Routes", "Route: Ruta1", "End of routes"]
    assert s.responder("mapObjectInfo habitacion1") == [
        'MapObjectInfo: "habitacion1" Goal ""',
        'MapObjectInfoCoord: "habitacion1" -8016 6148 0.000',
        "End of MapObjectInfo"]
    assert s.responder("mapObjectInfo noExiste") == ["End of MapObjectInfo"]


def test_igual_que_el_LD_errores():
    s = Sesion(robot())
    assert s.responder("noExiste") == ["Unknown command noExiste"]
    assert s.responder("") == [] and s.responder("   ") == []
    assert s.responder("goto noExisteGoal") == [
        "CommandError: goto noExisteGoal", "CommandErrorDescription: No goal 'noExisteGoal'"]
    assert s.responder("queuePickup noExisteGoal") == [
        "CommandError: queuePickup noExisteGoal",
        'CommandErrorDescription: queuepickup no such goal "noExisteGoal"']
    assert s.responder("queueQueryLocal xx yy")[1] == (
        "CommandErrorDescription: Valid querytypes are <id> <jobid> <robotname> <status> ")
    assert s.responder("queueCancelLocal jobid NOEXISTE") == [
        'queuecancel attempting to cancel "jobid" "NOEXISTE" "" "None"',
        'queuecancel cancelling "jobid" "NOEXISTE" "" "None" from queue',
        'queuecancel has failed to cancel "jobid" "NOEXISTE" "" "None" failure cause "no matching job"']
    assert s.responder("getInfo Inexistente") == [
        "CommandError: getInfo Inexistente", "CommandErrorDescription: No info 'Inexistente'"]
    assert s.responder("getInfo Queue ID") == ["Info: Queue ID "]
    assert s.responder("applicationBlockDrivingClear noExiste")[1] == (
        "CommandErrorDescription: There is no blockDriving noExiste to clear")


def test_igual_que_el_LD_ayuda_y_alias():
    s = Sesion(robot())
    assert s.responder("help queuePickup") == [
        "Commands and aliases with prefix 'queuePickup':",
        "    queuePickup          Queues a pickup goal for any appropriate robot [qp]",
        "queuePickupDropoff          Queues a pickup dropoff goal pair for any appropriate robot to do [qpd]",
        "End of commands"]
    assert s.responder("qs") == s.responder("queueShow")      # alias de help
    assert s.responder("STATUS") == s.responder("status")     # sin mayúsculas


def test_un_comando_sin_simular_lo_dice():
    s = Sesion(robot())
    assert s.responder("getDataStoreGroupList") == [
        "CommandError: getDataStoreGroupList",
        "CommandErrorDescription: getDataStoreGroupList no está implementado en el OMRON digital"]


# --- cola de trabajos ------------------------------------------------------------

def test_trabajo_de_la_cola_de_principio_a_fin():
    r = robot()
    s = Sesion(r)
    assert s.responder("queuePickup habitacion1 10 FM-1") == [
        'queuepickup goal "habitacion1" with priority 10 id PICKUP1 and job_id FM-1 successfully queued']
    eventos = correr(r, 30)          # incluye el QueueUpdate Pending de encolar
    estados = [e.split()[4] + " " + e.split()[5] for e in eventos if e.startswith("QueueUpdate:")]
    # visto en el LD (PRUEBA-1, 2026-11-20)
    assert estados == ["Pending None", "InProgress UnAllocated", "InProgress Allocated",
                       "InProgress Driving", "InProgress After", "Completed None"]
    assert all(e.endswith(" ") for e in eventos if e.startswith("QueueUpdate:"))
    # visto en el LD: al salir del dock repite la línea de queryDockStatus
    assert "DockingState: Undocking ForcedState: Unforced ChargeState: Float" in eventos
    assert "DockingState: Undocked ForcedState: Unforced ChargeState: Not" in eventos
    assert eventos.index("Going to habitacion1") < eventos.index("Arrived at habitacion1")
    assert (round(r.x), round(r.y)) == (-8016, 6148)
    assert r.docking == "Undocked"
    q = s.responder("queueQueryLocal jobid FM-1")
    assert q[-1] == "EndQueueQuery"
    assert q[0] == ('QueueQuery: PICKUP1 FM-1 10 Completed None Goal "habitacion1" "Omron-LD" '
                    '11/20/2026 09:03:04 11/20/2026 09:03:04 "" 0')
    assert s.responder("queueShowRobot")[0] == 'QueueRobot: "Omron-LD" Available Available ""'


def test_sin_localizar_no_coge_trabajos():
    r = robot(localizado=False)
    s = Sesion(r)
    s.responder("queuePickup descanso")
    correr(r, 10)
    assert r.cola[0].estado == "Pending"
    s.responder("localizeAtGoal descanso")
    correr(r, 10)
    assert r.cola[0].estado == "Completed"


def test_prioridad_y_orden_de_llegada():
    r = robot()
    s = Sesion(r)
    s.responder("queuePickup habitacion1 5 A")
    s.responder("queuePickup habitacion2 20 B")
    s.responder("queuePickup descanso 20 C")
    eventos = correr(r, 60)
    assert [t.estado for t in r.cola] == ["Completed"] * 3
    # B y C (prioridad 20, por orden de llegada) antes que A (prioridad 5)
    llegadas = [e for e in eventos if e.startswith("Arrived at")]
    assert llegadas == ["Arrived at habitacion2", "Arrived at descanso", "Arrived at habitacion1"]


def test_cancelar_un_trabajo_en_marcha():
    r = robot()
    s = Sesion(r)
    s.responder("queuePickup habitacion1 10 FM-2")
    correr(r, 14)
    assert r.cola[0].estado == "InProgress"
    salida = s.responder("queueCancelLocal jobid FM-2")
    assert len(salida) == 2                      # sin la línea de fallo
    assert r.cola[0].estado == "Cancelled" and not r.pasos
    pose = (r.x, r.y)
    correr(r, 5)
    assert (r.x, r.y) == pose


def test_queue_multi_comparte_job_id():
    r = robot()
    s = Sesion(r)
    salida = s.responder("queueMulti 2 2 descanso pickup habitacion2 dropoff FM-3")
    assert salida[-1] == "EndQueueMulti"
    assert [t.id for t in r.cola] == ["PICKUP1", "DROPOFF2"]
    assert {t.job_id for t in r.cola} == {"FM-3"}
    correr(r, 40)
    assert [t.estado for t in r.cola] == ["Completed", "Completed"]


# --- pausa, seta y paradas -----------------------------------------------------

def test_bloqueo_de_la_aplicacion_hace_de_pausa():
    # visto en el LD (PRUEBA-2): es un fallo que interrumpe y devuelve el trabajo
    # a la cola sin robot; al quitarlo, el robot lo vuelve a coger
    r = robot()
    s = Sesion(r)
    s.responder("queuePickup habitacion1")
    correr(r, 14)                    # 11 s para salir del dock, como el LD
    assert s.responder('applicationBlockDrivingSet fm "Pausa del FM" "Pausado desde el FM"') == [
        "ApplicationBlockDrivingSet set fm"]
    eventos = r.eventos + []
    assert "Interrupted: Going to habitacion1" in eventos
    assert (r.cola[0].estado, r.cola[0].subestado, r.cola[0].robot) == ("Pending", "Interrupted", "None")
    pose = (r.x, r.y)
    correr(r, 5)
    assert (r.x, r.y) == pose
    assert s.responder("status")[:2] == ["ExtendedStatusForHumans: Pausa del FM",
                                         "Status: Interrupted: Going to habitacion1"]
    assert s.responder("queueShowRobot")[0] == 'QueueRobot: "Omron-LD" UnAvailable Fault_Driving_Application_fm ""'
    assert s.responder("applicationBlockDrivingClear fm") == ["ApplicationBlockDrivingClear cleared fm"]
    correr(r, 30)
    assert r.cola[0].estado == "Completed"


def test_seta_para_el_robot_hasta_rearmar_motores():
    r = robot()
    s = Sesion(r)
    s.responder("queuePickup habitacion1")
    correr(r, 14)
    s.responder("digitalEstop on")
    assert s.responder("queueShowRobot")[0] == 'QueueRobot: "Omron-LD" UnAvailable EStopPressed ""'
    pose = (r.x, r.y)
    s.responder("digitalEstop off")
    correr(r, 5)
    assert (r.x, r.y) == pose                     # motores aún deshabilitados
    s.responder("enableMotors")
    correr(r, 30)
    assert r.cola[0].estado == "Completed"


def test_goto_directo_interrumpe_el_trabajo():
    r = robot()
    s = Sesion(r)
    s.responder("queuePickup habitacion1 10 FM-4")
    correr(r, 14)
    s.responder("goto descanso")
    eventos = correr(r, 1)
    assert any(" Interrupted " in e for e in eventos)


def test_patrol_once_recorre_la_ruta():
    r = robot()
    s = Sesion(r)
    s.responder("patrolOnce Ruta1")
    eventos = correr(r, 60)
    llegadas = [e for e in eventos if e.startswith("Arrived at")]
    assert llegadas == ["Arrived at descanso", "Arrived at habitacion1", "Arrived at habitacion2"]
    assert r.patrulla is None


# --- carga ------------------------------------------------------------------------

def test_autodock_por_bateria_y_sale_a_trabajar_cargando():
    r = robot(soc=24.0)
    Sesion(r).responder("localizeAtGoal descanso")      # fuera del dock
    eventos = correr(r, 30)
    # visto en el LD: Docking … Bulk y luego Docked … Bulk
    assert eventos[-2:] == ["DockingState: Docking ForcedState: Unforced ChargeState: Bulk",
                            "DockingState: Docked ForcedState: Unforced ChargeState: Bulk"]
    assert (r.docking, r.forzado, r.carga_estado) == ("Docked", "Unforced", "Bulk")
    s = Sesion(r)
    assert s.responder("queueShowRobot")[0] == 'QueueRobot: "Omron-LD" Available Available ""'
    # visto en el LD: aun con DockUntilDoneCharging, un trabajo lo saca del dock cargando
    s.responder("queuePickup habitacion1")
    correr(r, 40)
    assert r.cola[0].estado == "Completed"


def test_autodock_por_tiempo_parado():
    r = robot()
    poner_config(r.config["Docking"], "AutoDockIdleTime", "1")    # 1 minuto
    Sesion(r).responder("localizeAtGoal descanso")
    correr(r, 50)
    assert r.docking == "Undocked"
    correr(r, 30)
    assert r.docking == "Docked"


def test_sin_autodock_se_queda_donde_esta():
    r = robot(soc=10.0)
    poner_config(r.config["Docking"], "AutoDock", "false")
    Sesion(r).responder("localizeAtGoal descanso")
    correr(r, 30)
    assert r.docking == "Undocked"


def test_un_trabajo_saca_al_robot_que_va_a_cargar():
    r = robot(soc=24.0)
    s = Sesion(r)
    s.responder("localizeAtGoal habitacion1")
    correr(r, 1)
    assert r.docking == "Docking"
    s.responder("queuePickup habitacion2")
    correr(r, 1)
    assert r.cola[0].estado == "InProgress"


# --- servidor (socket de verdad) ------------------------------------------------

@pytest.fixture
def servidor():
    srv = ServidorARCL(("127.0.0.1", 0), robot(password="clave"), periodo=0.02, acelerar=20)
    srv.arrancar()
    yield srv
    srv.parar()


def _leer_hasta(sock: socket.socket, final: str, timeout: float = 3.0) -> str:
    sock.settimeout(timeout)
    datos = b""
    while final.encode() not in datos:
        trozo = sock.recv(65536)
        if not trozo:
            break
        datos += trozo
    return datos.decode()


def test_servidor_contrasena_mala_corta(servidor):
    c = socket.create_connection(("127.0.0.1", servidor.puerto))
    assert _leer_hasta(c, "Enter password:\r\n") == "Enter password:\r\n"
    c.sendall(b"mala\r\n")
    c.settimeout(3)
    try:
        datos = c.recv(100)          # como el LD: RST sin decir nada
    except ConnectionResetError:
        datos = b""
    assert datos == b""
    c.close()


def test_servidor_sesion_y_eventos_a_todos(servidor):
    a = socket.create_connection(("127.0.0.1", servidor.puerto))
    b = socket.create_connection(("127.0.0.1", servidor.puerto))
    for c in (a, b):
        _leer_hasta(c, "Enter password:\r\n")
        c.sendall(b"clave\r\n")
        bienvenida = _leer_hasta(c, "End of commands\r\n")
        assert bienvenida.startswith("Welcome to the server.\r\n")
    a.sendall(b"oneLineStatus\r\n")
    assert "ChargeState: Float StateOfCharge: 99.6" in _leer_hasta(a, "Temperature: 39\r\n")
    a.sendall(b"queuePickup descanso 10 FM-9\r\n")
    assert "successfully queued" in _leer_hasta(a, "successfully queued\r\n")
    # b no ha pedido nada, pero recibe los mensajes del robot
    assert "Arrived at descanso" in _leer_hasta(b, "Arrived at descanso\r\n", timeout=5)
    a.sendall(b"quit\r\n")
    assert _leer_hasta(a, "Closing connection\r\n").endswith("Closing connection\r\n")
    a.close()
    b.close()


def test_igual_que_el_LD_en_marcha_y_al_fallar():
    r = robot()
    s = Sesion(r)
    s.responder("localizeAtGoal descanso")
    s.responder("goto habitacion1")
    correr(r, 2)
    # visto en el LD, movido a mano desde MobilePlanner
    assert s.responder("modeQuery") == ['ModeQuery: Goal "Goal seeking" false true false']
    assert s.responder("queueShowRobot")[0] == 'QueueRobot: "Omron-LD" UnAvailable Interrupted ""'
    s.responder("digitalLost")
    s.responder("goto habitacion2")
    eventos = correr(r, 1)
    assert "Error: Failed going to goal" in eventos
    st = s.responder("status")
    assert st[0] == ("ExtendedStatusForHumans: Failed to get to habitacion2|Buffering: "
                     "Failed going to |Failed going to goal")
    assert st[1] == "Status: Error: Failed going to goal"
    assert s.responder("modeQuery") == ['ModeQuery: Goal "Goal seeking" false true false']


# --- macros y templates (vistos en el LD el 2026-11-20) -------------------------

MAPA_MACROS = """2D-Map-Ex4
Cairn: GoalWithHeading -8016 6148 0.0 "" ICON "Taller 2"
Cairn: GoalWithHeading -6239 1933 0.0 "" ICON "Almacen"
RouteInfo: GoalAfter "Almacen"
RouteInfo: Task "say" "en almacén"
RouteInfo: Macro "Macro1"
RouteInfo: GoalTask "goto" "Taller 2"
RouteInfo: _goto "Taller 2"
RouteInfo: _goalAfter "Taller 2"
RouteInfo: Task "wait" 5
RouteInfo: GoalTask "goto" "Almacen"
RouteInfo: _goto "Almacen"
RouteInfo: MacroTemplate "MacroTemplateFM"
RouteInfo: GoalTask "goto" "$1"
RouteInfo: _goto "$1"
RouteInfo: Task "wait" "$2"
LINES
DATA
"""


def robot_macros(tmp_path, **kw) -> OmronDigital:
    f = tmp_path / "macros.map"
    f.write_text(MAPA_MACROS, encoding="latin-1")
    kw.setdefault("reloj", lambda: FECHA)
    return OmronDigital(mapa=leer_mapa(f), **kw)


def test_mapa_con_macros_y_templates(tmp_path):
    m = robot_macros(tmp_path).mapa
    assert m.macros == {"Macro1": [("goto", "Taller 2"), ("wait", "5"), ("goto", "Almacen")]}
    assert m.templates == {"MacroTemplateFM": [("goto", "$1"), ("wait", "$2")]}


def test_template_con_parametros(tmp_path):
    r = robot_macros(tmp_path)
    s = Sesion(r)
    assert s.responder('executeMacroTemplate MacroTemplateFM "Taller 2" 3') == []
    eventos = correr(r, 60)
    assert eventos[0] == "Executing macro template MacroTemplateFM"
    assert 'WaitState: Waiting 3 seconds with status "Waiting"' in eventos
    assert eventos[-1] == "Completed macro template MacroTemplateFM"
    # dentro de una macro no hay "Going to"/"Arrived at"
    assert not any(e.startswith(("Going to", "Arrived at")) for e in eventos)
    assert (round(r.x), round(r.y)) == (-8016, 6148)
    assert s.responder("oneLineStatus")[0].startswith("Status: Completed macro template MacroTemplateFM ")


def test_template_con_goal_inexistente_termina_sin_error(tmp_path):
    r = robot_macros(tmp_path)
    s = Sesion(r)
    s.responder("localizeAtGoal Almacen")         # fuera del dock, como en la prueba real
    s.responder("executeMacroTemplate MacroTemplateFM NoExiste 5")
    eventos = correr(r, 2)
    assert eventos == ["Executing macro template MacroTemplateFM",
                       "Completed macro template MacroTemplateFM"]


def test_macro_y_listas(tmp_path):
    r = robot_macros(tmp_path)
    s = Sesion(r)
    assert s.responder("getMacros") == ["Macro: Macro1", "End of macros"]
    # visto en el LD: getMacroTemplates lista las macros, no los templates
    assert s.responder("getMacroTemplates") == ["MacroTemplate: Macro1", "End of MacroTemplate list"]
    assert s.responder("executeMacroTemplate PruebaFmMacro Almacen 5")[1] == (
        "CommandErrorDescription: No macro 'PruebaFmMacro Almacen 5'")
    s.responder("executeMacro Macro1")
    eventos = correr(r, 80)
    assert eventos[-1] == "Completed macro Macro1"


# --- más cosas vistas en el LD el 2026-11-20 ------------------------------------

def test_crear_goal_con_map_object_update():
    r = robot()
    s = Sesion(r)
    s.responder("localizeAtGoal descanso")
    s.responder("digitalEstop on")
    s.responder("digitalEstop off")              # visto en el LD: motores sin rearmar → no idle
    assert s.responder("mapObjectUpdate")[1] == "CommandErrorDescription: robot not idle, try again later"
    s.responder("enableMotors")
    assert s.responder("mapObjectUpdate")[1].startswith("CommandErrorDescription: argument error, expected:")
    assert s.responder('mapObjectUpdate GoalWithHeading 6165 -6505 -111 "" ICON "PruebaFM" 0 0 0 0') == [
        "Processing mapObjectUpdate, local map", "Completed mapObjectUpdate"]
    assert "Map changed" in r.eventos
    assert "Goal: PruebaFM" in s.responder("getGoals")


def test_seta_pulsada_como_el_LD():
    r = robot()
    s = Sesion(r)
    s.responder("digitalEstop on")
    assert r.eventos[-2:] == ["Motors disabled", "EStop pressed"]
    assert s.responder("modeQuery") == ['ModeQuery: MotorsAndEStop "EStop pressed" true false false']
    assert s.responder("status")[:2] == ["ExtendedStatusForHumans: EStop pressed", "Status: EStop pressed"]


def test_query_local_no_ve_el_trabajo_devuelto_a_la_cola():
    r = robot()
    s = Sesion(r)
    s.responder("queuePickup habitacion1 10 J1")
    correr(r, 14)
    s.responder('applicationBlockDrivingSet fm "Pausa" "Pausa"')
    assert s.responder("queueQueryLocal jobid J1") == ["EndQueueQuery"]
    assert s.responder("queueQuery jobid J1")[0].startswith("QueueQuery: PICKUP1 J1 10 Pending Interrupted")


def test_dock_con_nombre_en_el_mapa(tmp_path):
    f = tmp_path / "dock.map"
    f.write_text('2D-Map-Ex4\nCairn: DockLynx 6390 -5097 -178.0 "Lynx docking goal" ICON "DOCK"\n'
                 'Cairn: GoalWithHeading 7000 -4000 0.0 "" ICON "Taller"\nLINES\nDATA\n', encoding="latin-1")
    r = OmronDigital(mapa=leer_mapa(f), reloj=lambda: FECHA, soc=50)
    assert (r.x, r.y) == (6390, -5097)
    s = Sesion(r)
    assert s.responder("status")[0] == "ExtendedStatusForHumans: Charging, Connected to Charging Station DOCK"
    s.responder("goto Taller")
    correr(r, 2)
    assert s.responder("status")[0] == "ExtendedStatusForHumans: Decoupling from DOCK"
    correr(r, 20)
    s.responder("dock")
    correr(r, 1)
    assert s.responder("status")[0] == "ExtendedStatusForHumans: Driving to Charging Station DOCK"
