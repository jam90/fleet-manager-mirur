"""Benchmark: ¿convierte el intérprete una orden bien transcrita en el JSON correcto?

Cada caso es una orden limpia como "trae de la estantería 2 diferenciales". Usa
app/interprete.py (misiones de missions.json, vocabulario de vocab.json).
Se puntúa por campo y por orden completa.

    uv run --extra eval python -m eval.bench_extraction
    uv run --extra eval python -m eval.bench_extraction --no-corrected
"""

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

from app.interprete import CommandInterpreter, DEFAULT_MISSIONS

EVAL = Path(__file__).parent
PARAMS = ("object", "quantity", "zone")


def show(v):
    return "—" if v is None else str(v)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8001/v1")
    p.add_argument("--model", default=None, help="por defecto, el primer modelo del servidor")
    p.add_argument("--missions", default=str(DEFAULT_MISSIONS), help="ruta a missions.json o URL de /api/config")
    p.add_argument("--cases", nargs="+", default=[str(EVAL / "cases" / "extraccion.json")])
    p.add_argument("--no-guard", action="store_true", help="desactiva el veto de verbo y la comprobación de objeto/zona")
    p.add_argument("--no-corrected", action="store_true", help="el modelo no escribe su propia corrección (más rápido)")
    p.add_argument("--out", default=str(EVAL / "resultados" / "extraction.jsonl"))
    args = p.parse_args()

    interp = CommandInterpreter(args.missions, llm_url=args.url, model=args.model,
                                write_corrected=not args.no_corrected)
    cases = [c for path in args.cases for c in json.loads(Path(path).read_text())]
    interp.interpret("hola")  # calentamiento + nombre del modelo
    print(f"Modelo: {interp.model}  |  {len(cases)} casos  |  filtro={not args.no_guard}  corrected={not args.no_corrected}\n")

    rows = []
    print(f"{'#':<4} {'Frase':<44} {'Misión':<10} {'Objeto':<12} {'Cant':>4} {'Zona':<17} {'ms':>5}  OK")
    for c in cases:
        r = interp.interpret(c["text"], use_guard=not args.no_guard)
        fields = {k: r[k] == c[k] for k in PARAMS}
        row = {**c, "pred": {k: r[k] for k in ("mission", *PARAMS, "phrase", "action", "notes")},
               "mission_ok": r["mission"] == c["mission"], "fields_ok": fields,
               "latency_s": r["llm_ms"] / 1000, "parse_error": r["parse_error"]}
        row["all_ok"] = row["mission_ok"] and all(fields.values())
        rows.append(row)

        print(f"{c['id']:<4} {c['text'][:43]:<44} {r['mission']:<10} {show(r['object']):<12} "
              f"{show(r['quantity']):>4} {show(r['zone']):<17} {r['llm_ms']:>5.0f}  {'✓' if row['all_ok'] else '✗'}")
        if not row["all_ok"]:
            wrong = [k for k in PARAMS if not fields[k]] + ([] if row["mission_ok"] else ["mission"])
            exp = ", ".join(f"{k}={c[k]!r}" for k in wrong)
            print(f"     └ esperado {exp}" + (f"   ({c['note']})" if c.get("note") else ""))
        if r["notes"]:
            print(f"     · {'; '.join(r['notes'])}")

    n = len(rows)
    latencies = [r["latency_s"] for r in rows]
    summary = {
        "model": interp.model,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "guard": not args.no_guard,
        "corrected": not args.no_corrected,
        "n": n,
        "all_ok": sum(r["all_ok"] for r in rows),
        "mission_ok": sum(r["mission_ok"] for r in rows),
        **{f"{k}_ok": sum(r["fields_ok"][k] for r in rows) for k in PARAMS},
        "parse_errors": sum(r["parse_error"] for r in rows),
        "latency_mean_ms": statistics.mean(latencies) * 1000,
        "latency_p95_ms": sorted(latencies)[int(0.95 * (n - 1))] * 1000,
    }

    print(f"\n  Órdenes completas correctas : {summary['all_ok']}/{n}")
    print(f"  Misión                      : {summary['mission_ok']}/{n}")
    print(f"  Objeto                      : {summary['object_ok']}/{n}")
    print(f"  Cantidad                    : {summary['quantity_ok']}/{n}")
    print(f"  Zona                        : {summary['zone_ok']}/{n}")
    print(f"  Latencia del LLM            : media {summary['latency_mean_ms']:.0f} ms, p95 {summary['latency_p95_ms']:.0f} ms")
    if summary["parse_errors"]:
        print(f"  Errores de formato          : {summary['parse_errors']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.write(json.dumps({**summary, "rows": rows}, ensure_ascii=False) + "\n")
    print(f"\nResultados añadidos a {out}")


if __name__ == "__main__":
    main()
