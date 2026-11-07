"""SyncMonitor: la comprobación rápida no borra lo que sabía la completa (decisión 186)."""
import threading

from fm.web.sync_monitor import SyncMonitor


class FakeSync:
    """Lo que devolvería `check()`: la rápida solo dice si lo tiene."""
    def __init__(self):
        self.lock = threading.Lock()
        self.deep = {"m1": "distinta", "m2": "ok", "p1": "corregir"}
        self.quick = {"m1": "ok", "m2": "ok", "p1": "ok"}
        self.n = 0

    def check(self, deep=False):
        self.n += 1
        return {"level": "completa" if deep else "rápida", "checked_at": f"2026-11-07T10:00:{self.n:02d}Z",
                "state": dict(self.deep if deep else self.quick), "required": [], "outside": [], "surplus": []}


class Robot:
    last_telemetry = object()


def _monitor():
    sync = FakeSync()
    return SyncMonitor({"r1": sync, "r2": FakeSync()}, {"r1": Robot(), "r2": Robot()}, publish=lambda s, p: None), sync


def test_la_rapida_conserva_lo_de_la_completa_con_su_hora():
    mon, sync = _monitor()
    mon.check("r1", deep=True)
    assert mon.reports["r1"]["deep"] == {"m1": "2026-11-07T10:00:01Z", "m2": "2026-11-07T10:00:01Z",
                                         "p1": "2026-11-07T10:00:01Z"}
    mon.check("r1")
    rep = mon.reports["r1"]
    assert rep["level"] == "rápida" and rep["checked_at"] == "2026-11-07T10:00:02Z"
    assert rep["state"] == {"m1": "distinta", "m2": "ok", "p1": "corregir"}      # no se pierde
    assert rep["deep"]["m1"] == "2026-11-07T10:00:01Z"                            # de cuándo es


def test_lo_que_ya_no_tiene_o_ha_cambiado_se_olvida():
    mon, sync = _monitor()
    mon.check("r1", deep=True)
    sync.quick["m2"] = "falta"                     # lo borraron en su web
    mon.check("r1")
    sync.quick["m2"] = "ok"                        # y lo volvieron a crear: la completa no sabe cómo es
    mon.check("r1")
    rep = mon.reports["r1"]
    assert rep["state"]["m2"] == "ok" and "m2" not in rep["deep"]
    # Desplegado: igual que el catálogo (con la hora del despliegue). Falló: no se sabe.
    mon.same("r1", ["m1"])
    mon.forget(["p1"], ["r1"])
    mon.check("r1")
    rep = mon.reports["r1"]
    assert rep["state"] == {"m1": "ok", "m2": "ok", "p1": "ok"}
    assert "m1" in rep["deep"] and "p1" not in rep["deep"]


def test_traer_al_catalogo_cambia_lo_que_saben_los_demas():
    mon, _ = _monitor()
    mon.check("r1", deep=True)
    mon.check("r2", deep=True)
    mon.catalog_changed("r1", ["m1"])              # la versión de r1, ahora en el catálogo
    mon.check("r1")
    mon.check("r2")
    assert mon.reports["r1"]["state"]["m1"] == "ok" and "m1" in mon.reports["r1"]["deep"]
    assert "m1" not in mon.reports["r2"]["deep"]    # r2 tiene la anterior: hasta otra completa, no se sabe
    # Se recuerda de dónde vino y quién tiene aún la anterior (no se le ofrece traerla).
    assert mon.brought["m1"]["from"] == "r1" and mon.brought["m1"]["pending"] == ["r2"]
    mon.same("r2", ["m1"])                          # desplegada en r2: ya tiene la del catálogo
    assert mon.brought["m1"]["pending"] == []


def test_una_completa_pedida_durante_otra_va_despues():
    mon, sync = _monitor()
    mon.running.add("r1")                           # hay una rápida en curso
    assert mon.check_async("r1") is False           # otra rápida no hace falta
    assert mon.check_async("r1", deep=True) is True and "r1" in mon.deep_wanted
    mon.running.discard("r1")
    done = threading.Event()
    mon.publish = lambda s, p: p["report"]["level"] == "completa" and done.set()
    mon.check("r1")                                 # al acabar esta, lanza la completa
    assert done.wait(2)
