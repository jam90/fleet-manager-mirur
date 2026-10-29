import pytest

from app.logic import decide, levenshtein


def test_levenshtein():
    assert levenshtein("tonto", "tonto") == 0
    assert levenshtein("tonto", "tanto") == 1
    assert levenshtein("tonto", "pronto") == 2


def test_activacion_y_orden():
    r = decide(" Tonto, coge la pieza.\n")
    assert r["wake"] and r["command"] == "coge la pieza"
    assert (r["kind"], r["actionType"]) == ("mission", "coger")


def test_solo_activacion_arma():
    r = decide("Tonto.")
    assert r["wake"] and r["command"] == "" and r["mission"] is None


def test_sin_palabra_de_activacion():
    r = decide("Coge la pieza.")
    assert not r["wake"] and r["actionType"] == "coger"  # la página lo ignora salvo en ARMADO


@pytest.mark.parametrize("text", ["Tanto, deja la pieza.", "Tonta, déjala ahí.", "Tono deja la pieza"])
def test_variantes_de_activacion(text):
    r = decide(text)
    assert r["wake"] and r["actionType"] == "dejar"


def test_activacion_debe_ir_primero():
    assert not decide("Coge la pieza, tonto.")["wake"]


def test_confirmacion():
    assert decide("Adelante.")["confirm"]
    assert decide("¡Adelánte!")["confirm"]
    assert not decide("Atrás.")["confirm"]


def test_parar():
    r = decide("Tonto, para.")
    assert r["wake"] and r["kind"] == "stop" and r["mission"] == "parar"


def test_no_entendido():
    r = decide("Tonto, haz un baile.")
    assert r["wake"] and r["kind"] is None
