"""Genera el set de audio de prueba: TTS local con Piper + ruido, más grabaciones reales.

Cada orden distinta de cases/*.json se dice con la palabra de activación ("tonto, ...") en
N variantes aleatorias (voz × ruido × velocidad, semilla fija). El ruido de murmullo se hace con
frases cotidianas (no órdenes) sintetizadas por debajo. Salida: audio_cases/tts/*.wav +
audio_cases/manifest.json (fuera de git: se regenera).

Las voces Piper se descargan de Hugging Face la primera vez (audio_cases/voices/, ~300 MB).
Las grabaciones reales se buscan en --muestras-reales (carpetas coger/ y dejar/); si no existen,
se omiten.

    uv run --extra eval python -m eval.make_audio               # 3 variantes por frase
    uv run --extra eval python -m eval.make_audio --variants 6
"""

import argparse
import json
import os
import random
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf
from piper import PiperVoice, SynthesisConfig
from scipy.signal import resample_poly

EVAL = Path(__file__).parent
OUT = EVAL / "audio_cases"
VOICES_DIR = OUT / "voices"
VOICES_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/es"
# Por defecto, las muestras del repo hermano whisper-tests (robotica/whisper-tests/samples).
DEFAULT_REAL_DIR = os.environ.get("MUESTRAS_REALES", str(EVAL.parents[3] / "whisper-tests" / "samples"))
WAKE = "tonto, "
SR = 16000

# (archivo, id de locutor o None, etiqueta, ruta en piper-voices)
VOICES = [
    ("es_ES-davefx-medium", None, "es_ES_davefx_M", "es_ES/davefx/medium"),
    ("es_ES-sharvard-medium", 0, "es_ES_sharvard_M", "es_ES/sharvard/medium"),
    ("es_ES-sharvard-medium", 1, "es_ES_sharvard_F", "es_ES/sharvard/medium"),
    ("es_MX-ald-medium", None, "es_MX_ald_M", "es_MX/ald/medium"),
    ("es_AR-daniela-high", None, "es_AR_daniela_F", "es_AR/daniela/high"),
]
NOISES = {"clean": None, "white15": 15, "babble5": 5}  # valor = SNR en dB
SPEEDS = [0.9, 1.0, 1.1]  # length_scale de piper: >1 más lento
FIELDS = ("mission", "object", "quantity", "zone")
# Charla de fondo para el murmullo: frases normales, nunca órdenes al robot (así las etiquetas no son ambiguas).
CHATTER = [
    "mañana tenemos reunión a las diez", "el autobús llega tarde otra vez", "has visto el partido de ayer",
    "voy a por un café a la máquina", "el informe está casi terminado", "hace mucho calor en el taller hoy",
    "luego te paso las fotos del viaje", "la impresora no funciona bien", "comemos juntos en la cafetería",
    "el profesor explicó la práctica nueva", "me he dejado el móvil en casa", "este fin de semana voy al monte",
]


def download_voices():
    """Descarga de Hugging Face las voces que falten (modelo .onnx + su .onnx.json)."""
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    for name, _, _, hf_path in VOICES:
        for ext in (".onnx", ".onnx.json"):
            dest = VOICES_DIR / f"{name}{ext}"
            if not dest.exists():
                print(f"descargando {dest.name}…")
                urllib.request.urlretrieve(f"{VOICES_URL}/{hf_path}/{name}{ext}", dest)


def load_phrases():
    """Órdenes distintas bien dichas, con sus etiquetas."""
    phrases = {}
    for name in ("traer_dejar", "parametricos", "parar", "otros_verbos"):
        for c in json.loads((EVAL / "cases" / f"{name}.json").read_text()):
            phrases.setdefault(c["expected"], {"source": f"{name}:{c['id']}", **{k: c.get(k) for k in FIELDS}})
    for c in json.loads((EVAL / "cases" / "extraccion.json").read_text()):
        phrases.setdefault(c["text"], {"source": f"extraccion:{c['id']}", **{k: c.get(k) for k in FIELDS}})
    return phrases


def synth(voice, speaker, text, speed):
    chunks = voice.synthesize(text, SynthesisConfig(speaker_id=speaker, length_scale=speed))
    audio = np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32)
    return resample_poly(audio, SR, voice.config.sample_rate).astype(np.float32)


def add_noise(signal, noise, snr_db):
    noise = np.resize(noise, signal.shape)
    p_sig, p_noise = np.mean(signal ** 2), np.mean(noise ** 2) + 1e-12
    mixed = signal + noise * np.sqrt(p_sig / (p_noise * 10 ** (snr_db / 10)))
    return (mixed / max(1.0, np.abs(mixed).max())).astype(np.float32)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--variants", type=int, default=3, help="variantes aleatorias de voz/ruido/velocidad por frase")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--muestras-reales", default=DEFAULT_REAL_DIR, help="carpeta con coger/ y dejar/ (grabaciones reales)")
    args = p.parse_args()
    rng = random.Random(args.seed)
    np_rng = np.random.default_rng(args.seed)

    download_voices()
    voices = {f: PiperVoice.load(str(VOICES_DIR / f"{f}.onnx")) for f, _, _, _ in VOICES}
    phrases = load_phrases()
    (OUT / "tts").mkdir(parents=True, exist_ok=True)

    # Reserva de murmullo: charla de fondo, varias voces superpuestas.
    babble_pool = []
    for _ in range(12):
        parts = [synth(voices[f], spk, rng.choice(CHATTER), 1.0) for f, spk, _, _ in rng.sample(VOICES, 3)]
        n = max(len(x) for x in parts) * 2
        mix = np.zeros(n, dtype=np.float32)
        for x in parts:
            start = rng.randrange(0, n - len(x))
            mix[start:start + len(x)] += x
        babble_pool.append(mix)

    manifest = []
    for i, (text, labels) in enumerate(phrases.items()):
        combos = rng.sample([(v, nz, sp) for v in VOICES for nz in NOISES for sp in SPEEDS], args.variants)
        for (vfile, spk, vlabel, _), noise, speed in combos:
            audio = synth(voices[vfile], spk, WAKE + text, speed)
            audio = np.concatenate([np.zeros(int(0.3 * SR), np.float32), audio, np.zeros(int(0.3 * SR), np.float32)])
            if noise == "white15":
                audio = add_noise(audio, np_rng.normal(0, 1, len(audio)).astype(np.float32), NOISES[noise])
            elif noise == "babble5":
                audio = add_noise(audio, rng.choice(babble_pool), NOISES[noise])
            name = f"p{i:03d}_{vlabel}_{noise}_s{speed}.wav"
            sf.write(OUT / "tts" / name, audio, SR)
            manifest.append({"file": f"tts/{name}", "set": "tts", "text": text, "voice": vlabel,
                             "noise": noise, "speed": speed, **labels})
        print(f"\r{i + 1}/{len(phrases)} frases", end="", flush=True)

    # Grabaciones reales (misión según la carpeta; sin palabra de activación, frases libres).
    real_dir = Path(args.muestras_reales)
    if real_dir.is_dir():
        for folder, mission in (("coger", "mission_2"), ("dejar", "mission_1")):
            for f in sorted((real_dir / folder).iterdir()):
                manifest.append({"file": str(f.resolve()), "set": "real", "text": None, "voice": f.stem, "noise": None,
                                 "speed": None, "source": f"reales:{folder}", "mission": mission,
                                 "object": None, "quantity": None, "zone": None, "fields_unknown": True})
    else:
        print(f"\nsin grabaciones reales: no existe {real_dir}")

    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    n_tts = sum(m["set"] == "tts" for m in manifest)
    print(f"\n{n_tts} audios TTS + {len(manifest) - n_tts} grabaciones reales -> {OUT / 'manifest.json'}")


if __name__ == "__main__":
    main()
