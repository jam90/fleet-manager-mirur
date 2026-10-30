"""`POST /api/orders/{id}/cancel` ("tonto, para"): la pasarela delega en el FM,
que cancela la order esté en su cola (aún sin robot) o ya en curso."""
from fastapi.testclient import TestClient

from app import main
from app.fm_client import FMError


def _client(monkeypatch, fake):
    monkeypatch.setattr(main, "fm_cancel_order", fake)
    monkeypatch.setattr(main, "log_event", lambda *a, **k: None)
    return TestClient(main.app)


def test_cancelar_una_order_en_cola(monkeypatch):
    calls = []

    async def fake(base_url, order_id):
        calls.append(order_id)
        return {"orderId": order_id, "status": "CANCELLED", "serial": None}

    r = _client(monkeypatch, fake).post("/api/orders/mm-1/cancel?fleet=sim")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED" and calls == ["mm-1"]


def test_el_error_del_fm_llega_a_la_pagina(monkeypatch):
    async def fake(base_url, order_id):
        raise FMError(409, "Fleet Manager: 'mm-2' ya terminó (FINISHED)")

    r = _client(monkeypatch, fake).post("/api/orders/mm-2/cancel")
    assert r.status_code == 409 and "ya terminó" in r.json()["detail"]
