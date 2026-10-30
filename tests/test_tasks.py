"""`SqliteTaskStore` (decisión 160): las tareas sobreviven a cerrar y abrir."""
import json

from fm.tasks import ASSIGNED, FAILED, FINISHED, QUEUED, SqliteTaskStore, Task
from fm.vda5050.order import parse_order


def _raw(order_id, update_id=0):
    return {"orderId": order_id, "orderUpdateId": update_id, "edges": [],
            "nodes": [{"nodeId": "N0", "sequenceId": 0, "released": True,
                       "actions": [{"actionType": "coger", "actionId": "a1", "blockingType": "HARD"}]}]}


def _task(order_id, t=1000.0, **kw):
    raw = _raw(order_id)
    return Task(parse_order(raw), "coger", t, raw=raw, **kw)


def test_se_guarda_y_se_recarga(tmp_path):
    db = tmp_path / "cola.sqlite"
    s = SqliteTaskStore(db)
    a, b, c = _task("a"), _task("b"), _task("c")
    for t in (a, b, c):
        s.add(t)
    b.status, b.serial, b.job_id = ASSIGNED, "mir-1", "17"
    s.save(b)
    c.status, c.finished_at = FINISHED, 1100.0
    s.save(c)
    assert [t.order_id for t in s.alive()] == ["a", "b"]
    s.close()

    s = SqliteTaskStore(db, keep_days=None)       # "reinicio del FM" (sin purgar: fechas de 1970)
    a2, b2 = s.alive()
    assert (a2.order_id, a2.status) == ("a", QUEUED) and a2.order.orderId == "a"
    assert (b2.status, b2.serial, b2.job_id) == (ASSIGNED, "mir-1", "17")
    assert s.find("c").status == FINISHED          # terminada: del fichero
    assert s.find("a") is a2                       # viva: el mismo objeto que se va a cambiar
    assert s.find("nada") is None


def test_la_actualizacion_conserva_el_sitio(tmp_path):
    s = SqliteTaskStore(tmp_path / "cola.sqlite")
    a, b = _task("a"), _task("b")
    s.add(a); s.add(b)
    a.raw = _raw("a", 1)
    a.order = parse_order(a.raw)
    s.save(a)
    s.close()
    s = SqliteTaskStore(tmp_path / "cola.sqlite")
    assert [(t.order_id, t.update_id) for t in s.alive()] == [("a", 1), ("b", 0)]


def test_remove_y_reintento_con_el_mismo_id(tmp_path):
    s = SqliteTaskStore(tmp_path / "cola.sqlite")
    a = _task("a")
    s.add(a)
    s.remove(a)                                    # REJECTED al llegar: sin rastro
    assert s.find("a") is None and s.alive() == []
    old = _task("a", status=FAILED, finished_at=1.0)
    s.add(old)
    new = _task("a", t=2000.0)                     # se puede volver a mandar tras FAILED
    s.add(new)
    assert s.find("a") is new


def test_purga_del_historico(tmp_path):
    db = tmp_path / "cola.sqlite"
    s = SqliteTaskStore(db, keep_days=None)
    day = 86400
    viejo, reciente, viva = _task("viejo"), _task("reciente"), _task("viva", t=0.0)
    for t in (viejo, reciente, viva):
        s.add(t)
    viejo.status, viejo.finished_at = FINISHED, 0.0
    reciente.status, reciente.finished_at = FINISHED, 25 * day
    s.save(viejo); s.save(reciente)
    s.close()
    s = SqliteTaskStore(db, keep_days=30, now=40 * day)
    assert s.find("viejo") is None and s.find("reciente") is not None
    assert [t.order_id for t in s.alive()] == ["viva"]    # una viva no se purga nunca


def test_el_fichero_guarda_la_order_tal_cual(tmp_path):
    db = tmp_path / "cola.sqlite"
    s = SqliteTaskStore(db)
    s.add(_task("a"))
    row = s.db.execute("SELECT payload FROM tasks").fetchone()
    assert json.loads(row[0]) == _raw("a")
