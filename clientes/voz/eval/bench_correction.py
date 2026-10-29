"""Benchmark: ¿recupera el intérprete las órdenes que Whisper transcribió mal?

Usa app/interprete.py, así que las órdenes salen de missions.json. Cada caso se puntúa de tres
formas a partir de una sola llamada al LLM:

- solo fonético : comparador por parecido de sonido, sin LLM
- solo LLM      : la respuesta tal cual del modelo
- combinado     : fonético + LLM vigilándose + filtro de campos   <- lo que se desplegaría

más el comparador actual (palabra exacta, app/logic.py) como referencia.

    uv run --extra eval python -m eval.bench_correction
    uv run --extra eval python -m eval.bench_correction --missions http://localhost:8000/api/config
    uv run --extra eval python -m eval.bench_correction --no-guard --no-corrected
"""

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

from app.interprete import CommandInterpreter, DEFAULT_MISSIONS

EVAL = Path(__file__).parent
PARAMS = ("object", "quantity", "zone")
DEFAULT_CASES = [str(EVAL / "cases" / f"{n}.json") for n in ("traer_dejar", "parametricos", "parar", "otros_verbos")]


def outcome(pred, expected):
    if pred == expected:
        return "ok"
    if pred == "none":
        return "repeat"
    return "WRONG"  # el robot hace algo que no se pidió: el fallo grave


def score(rows, key):
    return {o: sum(outcome(r[key], r["mission"]) == o for r in rows) for o in ("ok", "repeat", "WRONG")}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8001/v1")
    p.add_argument("--model", default=None, help="por defecto, el primer modelo del servidor")
    p.add_argument("--missions", default=str(DEFAULT_MISSIONS), help="ruta a missions.json o URL de /api/config")
    p.add_argument("--cases", nargs="+", default=DEFAULT_CASES)
    p.add_argument("--no-guard", action="store_true", help="desactiva el veto de verbo y la comprobación de objeto/zona")
    p.add_argument("--no-corrected", action="store_true", help="el modelo no escribe su propia corrección (más rápido)")
    p.add_argument("--out", default=str(EVAL / "resultados" / "correction.jsonl"))
    args = p.parse_args()

    interp = CommandInterpreter(args.missions, llm_url=args.url, model=args.model,
                                write_corrected=not args.no_corrected)
    cases = [c for path in args.cases for c in json.loads(Path(path).read_text())]
    unknown = {c["mission"] for c in cases} - set(interp.missions) - {"none"}
    if unknown:
        print(f"AVISO: hay casos con misiones que no están en {args.missions}: {unknown}")
    interp.interpret("hola")  # calentamiento + nombre del modelo
    print(f"Modelo: {interp.model}  |  {len(cases)} casos  |  misiones: {', '.join(interp.missions)}  |  "
          f"filtro={not args.no_guard}  corrected={not args.no_corrected}\n")

    mark = {"ok": "✓", "repeat": "·", "WRONG": "✗✗"}
    print(f"{'#':<4} {'Whisper':<26} {'Dashboard':<30} {'Base':<5}{'Fon':<5}{'LLM':<5}{'Final':<6}{'ms':>5}  Campos")
    rows = []
    for c in cases:
        r = interp.interpret(c["whisper"], use_guard=not args.no_guard)
        row = {**c, **{f"pred_{k}": r[k] for k in PARAMS},
               "baseline_mission": interp.baseline(c["whisper"]),
               "phonetic_mission": r["phonetic_mission"], "llm_mission": r["llm_mission"],
               "final_mission": r["mission"], "action": r["action"], "phrase": r["phrase"],
               "corrected": r["corrected"], "confidence": r["confidence"], "notes": r["notes"],
               "latency_s": r["llm_ms"] / 1000, "parse_error": r["parse_error"]}
        row["fields_ok"] = {k: r[k] == c.get(k) for k in PARAMS} if "object" in c else None
        row["all_ok"] = row["final_mission"] == c["mission"] and (row["fields_ok"] is None or all(row["fields_ok"].values()))
        rows.append(row)

        cols = "".join(f"{mark[outcome(row[k], c['mission'])]:<5}" for k in ("baseline_mission", "phonetic_mission", "llm_mission"))
        fields = "" if row["fields_ok"] is None else ("✓" if all(row["fields_ok"].values()) else "✗")
        print(f"{c['id']:<4} {c['whisper'][:25]:<26} {r['phrase'][:29]:<30} {cols}"
              f"{mark[outcome(r['mission'], c['mission'])]:<6}{r['llm_ms']:>5.0f}  {fields}")
        wrong = [f"{k}={r[k]!r} (esperado {c[k]!r})" for k in PARAMS if row["fields_ok"] and not row["fields_ok"][k]]
        if wrong:
            print(f"     └ {', '.join(wrong)}")
        if r["notes"]:
            print(f"     · {'; '.join(r['notes'])}")

    commands = [r for r in rows if r["mission"] != "none"]
    latencies = [r["latency_s"] for r in rows]
    with_fields = [r for r in commands if r["fields_ok"] is not None]
    methods = {"baseline": "baseline_mission", "phonetic": "phonetic_mission", "llm": "llm_mission", "combined": "final_mission"}
    summary = {
        "model": interp.model,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "missions": list(interp.missions),
        "guard": not args.no_guard,
        "corrected": not args.no_corrected,
        "n": len(rows),
        **{name: score(rows, key) for name, key in methods.items()},
        "per_mission": {m: score([r for r in rows if r["mission"] == m], "final_mission") for m in sorted({r["mission"] for r in rows})},
        "real": score([r for r in rows if r.get("origin") == "real"], "final_mission"),
        "n_real": sum(r.get("origin") == "real" for r in rows),
        "fully_correct": sum(r["all_ok"] for r in with_fields),
        "n_with_fields": len(with_fields),
        **{f"{k}_ok": sum(r["fields_ok"][k] for r in with_fields) for k in PARAMS},
        "parse_errors": sum(r["parse_error"] for r in rows),
        "latency_mean_ms": statistics.mean(latencies) * 1000,
        "latency_p95_ms": sorted(latencies)[int(0.95 * (len(latencies) - 1))] * 1000,
    }

    n = summary["n"]
    print(f"\n✓ acierta   · pide repetir   ✗✗ ACCIÓN ERRÓNEA (el robot hace algo que no se pidió)\n")
    print(f"  {'Método':<34} {'aciertos':>8} {'repetir':>7} {'ERRÓNEA':>7}")
    labels = {"baseline": "actual (palabra exacta)", "phonetic": "solo fonético (sin LLM)",
              "llm": "solo LLM", "combined": "COMBINADO"}
    for name, label in labels.items():
        s = summary[name]
        print(f"  {label:<34} {s['ok']:>5}/{n} {s['repeat']:>7} {s['WRONG']:>7}")
    print("\n  Combinado, por misión:")
    for m, s in summary["per_mission"].items():
        tot = sum(s.values())
        print(f"    {m:<12} {s['ok']}/{tot} aciertos, {s['repeat']} repetir, {s['WRONG']} ERRÓNEAS")
    s = summary["real"]
    print(f"    casos reales {s['ok']}/{summary['n_real']} aciertos, {s['repeat']} repetir, {s['WRONG']} ERRÓNEAS")
    if with_fields:
        k = summary["n_with_fields"]
        print(f"\n  Campos (combinado): {summary['fully_correct']}/{k} completos  ·  objeto {summary['object_ok']}/{k}"
              f"  cantidad {summary['quantity_ok']}/{k}  zona {summary['zone_ok']}/{k}")
    print(f"  Latencia del LLM: media {summary['latency_mean_ms']:.0f} ms, p95 {summary['latency_p95_ms']:.0f} ms")
    if summary["parse_errors"]:
        print(f"  Errores de formato: {summary['parse_errors']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.write(json.dumps({**summary, "rows": rows}, ensure_ascii=False) + "\n")
    print(f"\nResultados añadidos a {out}")


if __name__ == "__main__":
    main()
