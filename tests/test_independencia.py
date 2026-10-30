"""El core del FM no depende de las pasarelas de clientes/ (voz, visión…).

Igual que el core no importa ningún driver concreto, las pasarelas son clientes
del FM por su API REST/MQTT: el FM tiene que poder desplegarse sin ellas.
"""
import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nombres = set()
    for nodo in ast.walk(tree):
        if isinstance(nodo, ast.Import):
            nombres.update(a.name for a in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            nombres.add(nodo.module)
    return nombres


def test_fm_no_importa_clientes():
    archivos = [*(RAIZ / "fm").rglob("*.py"), RAIZ / "run_fm.py"]
    culpables = {
        str(f.relative_to(RAIZ)): sorted(m for m in _imports(f) if m.split(".")[0] in ("clientes", "app"))
        for f in archivos
    }
    assert not {f: m for f, m in culpables.items() if m}
