"""Misiones de un MiR para la pestaña Robot (decisión 113), sin red.

Las respuestas de ejemplo son copias recortadas de las de mir-1 (2026-10-23)."""
from fm.adapters.mir.missions import (MirMissions, render_message, run_from_detail, split_queue,
                                      step_from_detail, to_utc)

DETAIL = {"id": 1482, "mission_id": "g-move", "state": "Done",
          "message": "ActionList was executed without problems..",
          "ordered": "2026-10-23T08:07:16+00:00", "started": "2026-10-23T08:07:16+00:00",
          "finished": "2026-10-23T08:07:52+00:00",
          "parameters": [{"id": "Position", "value": "97d5-guid", "label": "Punto espera 2"},
                         {"id": "pieza", "value": 36}]}
STEP = {"id": 4464, "mission_queue_id": 1482, "state": "Succeded", "action_type": "move",
        "message": "{\"message\": \"Position '%(position_name)s' reached..\", \"args\": {\"position_name\":\"Punto espera 2\"}}",
        "started": "2026-10-23T08:07:16", "finished": "2026-10-23T08:07:52"}


def test_mensajes_del_mir_legibles():
    assert render_message(STEP["message"]) == "Position 'Punto espera 2' reached."
    assert render_message("ActionList was executed without problems..") == "ActionList was executed without problems."
    assert render_message("") == "" and render_message(None) == ""
    # Plantilla con un argumento que no viene: el texto sin rellenar, no una excepción
    assert render_message('{"message": "Hola %(x)s", "args": {}}') == "Hola %(x)s"


def test_horas_a_utc():
    assert to_utc("2026-10-23T08:07:16+00:00") == "2026-10-23T08:07:16Z"
    assert to_utc("2026-10-23T08:07:16") == "2026-10-23T08:07:16Z"       # los pasos vienen sin zona
    assert to_utc(None) is None


def test_entrada_y_paso():
    r = run_from_detail(DETAIL, "Move", own=True)
    assert (r.job_id, r.name, r.status, r.own) == ("1482", "Move", "FINISHED", True)
    assert r.message == "ActionList was executed without problems."
    assert r.parameters == [("Position", "Punto espera 2"), ("pieza", "36")]   # la etiqueta si la hay
    assert r.started == "2026-10-23T08:07:16Z" and r.steps == []
    s = step_from_detail(STEP)
    assert (s.kind, s.status, s.message) == ("move", "FINISHED", "Position 'Punto espera 2' reached.")
    # Estado desconocido: se enseña tal cual
    assert run_from_detail({**DETAIL, "state": "Abort"}, "x", False).status == "Abort"


def test_reparto_de_la_cola():
    q = [{"id": i, "state": s} for i, s in
         [(1, "Done"), (2, "Aborted"), (3, "Done"), (4, "Executing"), (5, "Pending"), (6, "Pending")]]
    current, pending, done = split_queue(q, history=2)
    assert current == 4 and pending == [5, 6] and done == [3, 2]
    assert split_queue([{"id": 1, "state": "Done"}], history=0) == (None, [], [])


class FakeClient:
    """Lo justo de `MirClient` para `MirMissions`, contando las peticiones."""

    def __init__(self):
        self.queue = [{"id": 1, "state": "Done"}, {"id": 2, "state": "Executing"}, {"id": 3, "state": "Pending"}]
        self.steps = {2: [{"id": 10, "state": "Succeded"}, {"id": 11, "state": "Executing"}]}
        self.calls = []

    def mission_queue_get(self):
        self.calls.append("queue")
        return [dict(e) for e in self.queue]

    def mission_queue_id_get(self, qid):
        self.calls.append(f"q{qid}")
        state = next(e["state"] for e in self.queue if e["id"] == qid)
        return {**DETAIL, "id": qid, "state": state, "mission_id": f"g{qid}"}

    def mission_queue_id_actions_get(self, qid):
        return [{"id": a["id"]} for a in self.steps.get(qid, [])]

    def mission_queue_id_actions_id_get(self, qid, aid):
        self.calls.append(f"a{aid}")
        a = next(a for a in self.steps[qid] if a["id"] == aid)
        return {**STEP, "id": aid, "state": a["state"]}

    def missions_mission_id_get(self, guid):
        self.calls.append(guid)
        return {"name": f"mission {guid}"}


def test_vista_y_cache():
    c = FakeClient()
    m = MirMissions(c, is_own=lambda q: q == 1)
    v = m.view()
    assert v.current.job_id == "2" and v.current.status == "RUNNING" and v.current.name == "mission g2"
    assert [s.status for s in v.current.steps] == ["FINISHED", "RUNNING"]
    assert [r.job_id for r in v.pending] == ["3"] and v.pending[0].status == "WAITING"
    assert [r.job_id for r in v.history] == ["1"] and v.history[0].own
    # Segunda consulta: la terminada (q1), su nombre y el paso terminado (a10) no se vuelven a pedir;
    # la en curso, la de la cola y el paso en marcha, sí.
    c.calls.clear()
    m.view()
    assert "q1" not in c.calls and "g1" not in c.calls and "a10" not in c.calls
    assert {"queue", "q2", "q3", "a11"} <= set(c.calls)


def test_sin_nada_en_marcha():
    c = FakeClient()
    c.queue = [{"id": 1, "state": "Done"}]
    v = MirMissions(c).view()
    assert v.current is None and v.pending == [] and not v.history[0].own


def test_desfase_del_reloj_del_robot():
    from fm.adapters.mir.missions import clock_offset
    from email.utils import formatdate
    now = 1_800_000_000.0
    # mir-2 el 2026-10-23: 5 min 27 s atrasado
    assert clock_offset(formatdate(now - 327, usegmt=True), now) == -327
    assert clock_offset(None, now) is None and clock_offset("basura", now) is None
    c = FakeClient()
    c.last_date = formatdate(now, usegmt=True)
    v = MirMissions(c).view()
    assert v.clock_offset_s is not None and abs(v.clock_offset_s) < 10 ** 9   # medido contra time.time()
