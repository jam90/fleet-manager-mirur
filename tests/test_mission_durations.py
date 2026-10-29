"""Cálculos de `scripts/mission_durations.py` (sin robot)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from mission_durations import percentile, propose, run_seconds  # noqa: E402


def test_tiempo_de_una_entrada():
    # Las entradas de la cola llevan zona (+00:00); sin started o finished, None.
    assert run_seconds({"started": "2026-10-23T10:00:00+00:00", "finished": "2026-10-23T10:01:30+00:00"}) == 90
    assert run_seconds({"started": "2026-10-23T10:00:00+00:00", "finished": None}) is None
    assert run_seconds({}) is None


def test_propuesta_p90_por_margen_y_minimo_de_muestras():
    assert percentile([10, 20, 30, 40, 50], 0.9) == 46.0
    assert propose([10, 20, 30, 40, 50]) == 70        # 46 × 1,5 = 69 → 70
    assert propose([50, 50, 60, 70]) is None           # menos de 5: a mano
