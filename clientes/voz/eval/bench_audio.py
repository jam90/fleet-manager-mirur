"""Benchmark del camino de voz completo con audio (TTS + ruido y grabaciones reales).

Caminos (pipelines):
  p0   hoy antes     : whisper-server, prompt="tonto", greedy               -> intérprete
  p1   opción A      : whisper-server, prompt = palabra de activación + órdenes de ejemplo construidas
                       con missions.json/vocab, beam_size=5, probabilidades por palabra -> intérprete,
                       más "pedir repetir si el verbo se oyó con probabilidad baja" (--verb-prob)
  p1g  opción A      : igual que p1 pero greedy
  p1w  opción A'     : el prompt es una lista de palabras sueltas, beam 5
  p1wg opción A'     : lista de palabras sueltas, greedy  <- lo que usa hoy la pasarela
  p2   opción B      : un modelo de audio (servidor OpenAI-compatible con input_audio, p. ej. llama-server
                       con Gemma 4 E2B) oye el audio y devuelve el JSON de la orden + la transcripción,
                       y después el mismo comparador fonético y los mismos vetos

    uv run --extra eval python -m eval.make_audio
    uv run --extra eval python -m eval.bench_audio --pipelines p0,p1wg
    uv run --extra eval python -m eval.bench_audio --pipelines p2 --audio-llm-url http://localhost:8002/v1
"""

import argparse
import base64
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

import httpx
import jiwer

from app.config import settings
from app.interprete import CommandInterpreter, DEFAULT_MISSIONS, normalize, similarity
from app.logic import is_word
from eval.bench_correction import outcome

EVAL = Path(__file__).parent
PARAMS = ("object", "quantity", "zone")


def strip_wake(text):
    """Misma regla que app/logic.py: quita la primera palabra si es la palabra de activación."""
    raw = text.split()
    first = normalize(raw[0]) if raw else ""
    wake = bool(first) and is_word(first, settings.wake_word, settings.wake_variants)
    return (" ".join(raw[1:]) if wake else text), wake


def whisper_prompt(interp):
    """Palabra de activación + un ejemplo por misión, cubriendo todos los objetos y zonas del vocabulario."""
    objs, zones = interp.vocab["objects"], interp.vocab["zones"]
    examples, k = [], 0
    for name, m in interp.missions.items():
        if m.get("instant"):
            examples += m["words"][:2]
            continue
    while k < max(len(objs), len(zones)):
        for name, m in interp.missions.items():
            if m.get("instant") or k >= max(len(objs), len(zones)):
                continue
            examples.append(interp.phrase(name, objs[k % len(objs)], 1 + k % 3, zones[k % len(zones)]))
            k += 1
    return f"{settings.wake_word}, " + ". ".join(examples) + "."


def whisper_wordlist(interp):
    """Palabra de activación + palabras sueltas del vocabulario (sin frases: menos riesgo de que Whisper invente una orden)."""
    words = [w for m in interp.missions.values() for w in m["words"]] + interp.vocab["objects"] + interp.vocab["zones"]
    return f"{settings.wake_word}, " + ", ".join(dict.fromkeys(words)) + "."


def merge_words(segments):
    """whisper-server devuelve trozos de palabra; se unen en palabras (palabra nueva = empieza por espacio)."""
    words = []
    for seg in segments:
        for t in seg.get("words", []):
            piece = t["word"]
            if piece.startswith(" ") or not words:
                words.append({"word": piece.strip(), "p": t["probability"]})
            else:
                words[-1]["word"] += piece
                words[-1]["p"] = min(words[-1]["p"], t["probability"])
    return [w for w in words if normalize(w["word"])]


def transcribe(url, path, prompt, beam_size=None):
    data = {"temperature": "0.0", "language": "es", "prompt": prompt,
            "response_format": "verbose_json" if beam_size else "json"}
    if beam_size:
        data["beam_size"] = str(beam_size)
    start = time.perf_counter()
    with open(path, "rb") as f:
        r = httpx.post(url, files={"file": f}, data=data, timeout=120)
    r.raise_for_status()
    body = r.json()
    return body.get("text", "").strip(), merge_words(body.get("segments", [])), (time.perf_counter() - start) * 1000


def verb_probability(interp, words, command):
    """Probabilidad que dio Whisper a la palabra que el comparador fonético tomó como verbo."""
    cmd_words = normalize(command).split()[:3]
    forms = [f for fs in interp.forms.values() for f in fs]
    best = max(cmd_words, key=lambda w: max(similarity(w, f) for f in forms), default=None)
    probs = [w["p"] for w in words if normalize(w["word"]) == best]
    return probs[0] if probs else None


def ask_audio_llm(client_url, model, interp, path):
    audio = base64.b64encode(Path(path).read_bytes()).decode()
    fmt = Path(path).suffix.lstrip(".").replace("m4a", "mp4")
    system = interp.prompt + (f'\n\nRecibes AUDIO, no texto. Ignora la palabra de activación "{settings.wake_word}" del principio. '
                              'En "corrected" escribe lo que dijo el usuario (sin la palabra de activación).')
    start = time.perf_counter()
    r = httpx.post(f"{client_url}/chat/completions", timeout=120, json={
        "model": model or "default",
        "temperature": 0.0,
        "max_tokens": 192,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": audio, "format": fmt}}]}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "command", "schema": interp.schema}},
    })
    ms = (time.perf_counter() - start) * 1000
    r.raise_for_status()
    try:
        return json.loads(r.json()["choices"][0]["message"]["content"]), ms
    except (json.JSONDecodeError, KeyError, TypeError):
        return {"mission": "none", "parse_error": True}, ms


def run(pipeline, entries, interp, args, prompts):
    rows = []
    for i, e in enumerate(entries):
        path = e["file"] if e["set"] == "real" else str(EVAL / "audio_cases" / e["file"])
        row = {"pipeline": pipeline, **e}
        if pipeline in prompts:
            prompt, beam = prompts[pipeline]
            text, words, asr_ms = transcribe(args.whisper_url, path, prompt, beam)
            command, wake = strip_wake(text)
            r = interp.interpret(command)
            row.update(transcript=text, wake_ok=wake, asr_ms=asr_ms,
                       exact_outcome=outcome(interp.baseline(command), e["mission"]))
            if beam:
                p = verb_probability(interp, words, command)
                row["verb_p"] = p
                if args.verb_prob and r["mission"] != "none" and p is not None and p < args.verb_prob:
                    r["notes"].append(f"verbo oído con p={p:.2f} < {args.verb_prob}")
                    r.update(mission="none", action="repeat")
        else:
            llm, ms = ask_audio_llm(args.audio_llm_url, args.audio_llm_model, interp, path)
            command = llm.get("corrected") or ""
            r = interp.decide(command, llm, llm_ms=ms)
            row.update(transcript=command, wake_ok=None, asr_ms=0.0)
        row.update(command=command, final_mission=r["mission"], llm_mission=r["llm_mission"],
                   phonetic_mission=r["phonetic_mission"], action=r["action"], phrase=r["phrase"],
                   notes=r["notes"], llm_ms=r["llm_ms"], **{f"pred_{k}": r[k] for k in PARAMS})
        row["outcome"] = outcome(r["mission"], e["mission"])
        if not e.get("fields_unknown"):
            row["fields_ok"] = all(r[k] == e[k] for k in PARAMS)
        if e["text"]:
            ref, hyp = normalize(e["text"]), normalize(command)
            row["wer"] = jiwer.wer(ref, hyp) if hyp else 1.0
        rows.append(row)
        print(f"\r  {pipeline}: {i + 1}/{len(entries)}", end="", flush=True)
    print()
    return rows


def summarize(pipeline, rows):
    out = {"pipeline": pipeline}
    for s in ("tts", "real"):
        sub = [r for r in rows if r["set"] == s]
        if not sub:
            continue
        out[s] = {o: sum(r["outcome"] == o for r in sub) for o in ("ok", "repeat", "WRONG")} | {"n": len(sub)}
    for key, name in (("outcome", "decisor"), ("exact_outcome", "exact")):
        if all(key in r for r in rows):
            out[name] = {o: sum(r[key] == o for r in rows) for o in ("ok", "repeat", "WRONG")} | {"n": len(rows)}
    tts = [r for r in rows if r["set"] == "tts"]
    if tts:
        out["fields_ok"] = sum(r.get("fields_ok", False) and r["outcome"] == "ok" for r in tts)
        out["wer"] = statistics.mean(r["wer"] for r in tts)
        out["wake_missed"] = sum(r["wake_ok"] is False for r in tts)
    out["latency_ms"] = statistics.mean(r["asr_ms"] + r["llm_ms"] for r in rows)
    out["asr_ms"] = statistics.mean(r["asr_ms"] for r in rows)
    out["llm_ms"] = statistics.mean(r["llm_ms"] for r in rows)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pipelines", default="p0,p1wg,p1w", help="p0, p1, p1g, p1w, p1wg, p2")
    p.add_argument("--set", choices=["all", "tts", "real"], default="all")
    p.add_argument("--limit", type=int, default=0, help="solo los N primeros audios (prueba rápida)")
    p.add_argument("--whisper-url", default="http://localhost:8080/inference")
    p.add_argument("--llm-url", default="http://localhost:8001/v1", help="LLM de texto para p0/p1")
    p.add_argument("--audio-llm-url", default="http://localhost:8002/v1", help="modelo de audio para p2")
    p.add_argument("--audio-llm-model", default=None)
    p.add_argument("--missions", default=str(DEFAULT_MISSIONS))
    p.add_argument("--verb-prob", type=float, default=0.0,
                   help="caminos con beam: repetir si la probabilidad del verbo es menor (0 = apagado; 0.5 empeoró)")
    p.add_argument("--out", default=str(EVAL / "resultados" / "audio.jsonl"))
    args = p.parse_args()

    entries = json.loads((EVAL / "audio_cases" / "manifest.json").read_text())
    entries = [e for e in entries if args.set == "all" or e["set"] == args.set]
    if args.limit:
        entries = entries[:args.limit]
    interp = CommandInterpreter(args.missions, llm_url=args.llm_url)
    sentences, wordlist = whisper_prompt(interp), whisper_wordlist(interp)
    # camino -> (prompt de whisper, tamaño de beam o None para greedy)
    prompts = {"p0": (settings.wake_word, None), "p1": (sentences, 5), "p1g": (sentences, None),
               "p1w": (wordlist, 5), "p1wg": (wordlist, None)}
    pipelines = args.pipelines.split(",")
    if set(prompts) & set(pipelines):
        interp.interpret("hola")  # calentamiento + nombre del modelo
    print(f"{len(entries)} audios  |  caminos {pipelines}")
    for k in pipelines:
        if k in prompts:
            print(f"  {k}: beam={prompts[k][1] or 1}  prompt: {prompts[k][0]}")
    print()

    summaries, all_rows = [], []
    for pl in pipelines:
        rows = run(pl, entries, interp, args, prompts)
        all_rows += rows
        summaries.append(summarize(pl, rows))

    labels = {"p0": "antes (tonto, greedy)", "p1": "frases, beam 5", "p1g": "frases, greedy",
              "p1w": "palabras, beam 5", "p1wg": "palabras, greedy", "p2": "B: modelo de audio"}
    fmt = lambda d: f"{d['ok']}/{d['repeat']}/{d['WRONG']}" if d else "-"
    print(f"\nacierta/repetir/ERRÓNEA sobre {len(entries)} audios (ERRÓNEA = el robot hace algo que no se pidió)\n")
    print(f"  {'Prompt de Whisper':<22} {'exacta':>11} {'decisor':>11} {'real (dec.)':>11} {'WER':>5} {'activ✗':>6} "
          f"{'whisper ms':>10} {'LLM ms':>7}")
    for s in summaries:
        print(f"  {labels[s['pipeline']]:<22} {fmt(s.get('exact')):>11} {fmt(s.get('decisor')):>11} {fmt(s.get('real')):>11} "
              f"{s.get('wer', 0):>5.2f} {s.get('wake_missed', '-'):>6} {s['asr_ms']:>10.0f} {s['llm_ms']:>7.0f}")

    print("\n  Acciones erróneas:")
    for r in all_rows:
        who = [n for k, n in (("exact_outcome", "exacta"), ("outcome", "decisor")) if r.get(k) == "WRONG"]
        if who:
            print(f"   {r['pipeline']:<5} {'+'.join(who):<14} {r['voice']:<18} {str(r['noise']):<8} "
                  f"dicho={r['text']!r} oído={r['transcript']!r}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now().isoformat(timespec="seconds"), "whisper_configs": prompts,
                            "verb_prob": args.verb_prob, "summaries": summaries, "rows": all_rows}, ensure_ascii=False) + "\n")
    print(f"\nResultados añadidos a {out}")


if __name__ == "__main__":
    main()
