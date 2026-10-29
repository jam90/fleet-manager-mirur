import pytest

from app.logic import decide, levenshtein


def test_levenshtein():
    assert levenshtein("tonto", "tonto") == 0
    assert levenshtein("tonto", "tanto") == 1
    assert levenshtein("tonto", "pronto") == 2


def test_wake_and_command():
    r = decide(" Tonto, coge la pieza.\n")
    assert r["wake"] and r["command"] == "coge la pieza"
    assert (r["kind"], r["actionType"]) == ("mission", "coger")


def test_wake_only_arms():
    r = decide("Tonto.")
    assert r["wake"] and r["command"] == "" and r["mission"] is None


def test_without_wake_word():
    r = decide("Coge la pieza.")
    assert not r["wake"] and r["actionType"] == "coger"  # the page ignores it unless ARMED


@pytest.mark.parametrize("text", ["Tanto, deja la pieza.", "Tonta, déjala ahí.", "Tono deja la pieza"])
def test_wake_variants(text):
    r = decide(text)
    assert r["wake"] and r["actionType"] == "dejar"


def test_wake_word_must_be_first():
    assert not decide("Coge la pieza, tonto.")["wake"]


def test_confirm():
    assert decide("Adelante.")["confirm"]
    assert decide("¡Adelánte!")["confirm"]
    assert not decide("Atrás.")["confirm"]


def test_stop():
    r = decide("Tonto, para.")
    assert r["wake"] and r["kind"] == "stop" and r["mission"] == "parar"


def test_not_understood():
    r = decide("Tonto, haz un baile.")
    assert r["wake"] and r["kind"] is None
