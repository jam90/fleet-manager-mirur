"""Intérprete de órdenes tolerante a errores de Whisper, construido a partir de la lista de misiones.

EXPERIMENTAL: no se usa desde app/main.py. Se evalúa con eval/ (ver docs/PROPUESTA_DETECCION_VOZ.md).

Todo lo que depende de las órdenes (comparador fonético, coincidencia exacta de referencia,
prompt y esquema JSON del LLM, frase para el dashboard) se genera desde missions.json y se
reconstruye solo cuando cambia la lista, así que añadir una misión no requiere tocar código.

    interp = CommandInterpreter("missions.json")                          # archivo, se relee si cambia
    interp = CommandInterpreter("http://localhost:8000/api/config")       # API de esta pasarela
    result = interp.interpret("trae de la estarentia 2 diferencuiales")
    # {"mission": "mission_2", "object": "diferencial", "quantity": 2, "zone": "estantería",
    #  "phrase": "trae dos diferenciales de la estantería", "action": "execute", ...}

Decisión = comparador fonético (verbo) + LLM (campos), cada uno puede vetar al otro:
- los dos nombran una misión y no coinciden       -> repetir
- solo el LLM nombra una misión                   -> esa misión, salvo que la frase suene
                                                     claramente a otra orden
- solo el comparador fonético nombra una          -> esa misión solo si el parecido es fuerte
                                                     (un verbo desconocido como "lleva" no debe acabar en "deja")
- orden inmediata (parar) pero se oye un objeto   -> repetir
- objeto/zona que no se oyen en la frase          -> se descartan (sin objeto -> repetir)
"""

import json
import os
import re
import time
import unicodedata
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

DEFAULT_MISSIONS = Path(__file__).parent.parent / "missions.json"
DEFAULT_VOCAB = Path(__file__).parent.parent / "vocab.json"

PHONETIC_MARGIN = 0.10  # ventaja mínima sobre el segundo para que el comparador fonético decida
VETO_MARGIN = 0.15      # se veta una misión si otra suena al menos esto más parecida
STRONG_MATCH = 0.70     # el comparador fonético solo actúa solo (LLM dijo none) por encima de esto
FIELD_MATCH = 0.72      # objeto/zona deben sonar al menos así de parecidos a una palabra de la frase
NUMBERS = ["", "", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez"]


def normalize(text):
    """Igual que logic.normalize: minúsculas, sin tildes, sin puntuación."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


class MissionSource:
    """Ruta a missions.json o URL de /api/config. `get()` devuelve (misiones, cambiado)."""

    def __init__(self, source=None, ttl_s=5.0):
        self.source = str(source or os.environ.get("MISSIONS_SOURCE", DEFAULT_MISSIONS))
        self.ttl_s = ttl_s
        self._stamp = None
        self._checked = 0.0
        self._missions = None

    def _read(self):
        if self.source.startswith(("http://", "https://")):
            with urllib.request.urlopen(self.source, timeout=3) as r:
                data = json.load(r)
            return data.get("missions", data), None
        path = Path(self.source)
        return json.loads(path.read_text(encoding="utf-8")), path.stat().st_mtime

    def get(self):
        is_url = self.source.startswith(("http://", "https://"))
        if self._missions is not None:
            if is_url and time.monotonic() - self._checked < self.ttl_s:
                return self._missions, False
            if not is_url and Path(self.source).stat().st_mtime == self._stamp:
                return self._missions, False
        try:
            missions, stamp = self._read()
        except Exception:
            if self._missions is None:
                raise
            return self._missions, False  # si la fuente no responde, se sigue con la última lista buena
        self._checked = time.monotonic()
        changed = missions != self._missions
        self._missions, self._stamp = missions, stamp
        return missions, changed


class CommandInterpreter:
    def __init__(self, missions=None, vocab=DEFAULT_VOCAB, llm_url="http://localhost:8001/v1",
                 model=None, write_corrected=True):
        self.source = MissionSource(missions)
        self.vocab = json.loads(Path(vocab).read_text(encoding="utf-8"))
        self.llm_url = llm_url
        self.model = model
        self.write_corrected = write_corrected  # el modelo escribe antes su corrección (le ayuda a pensar)
        self._client = None
        self.refresh(force=True)

    # ---- todo se construye desde la lista de misiones ------------------------------------

    def refresh(self, force=False):
        missions, changed = self.source.get()
        if not (changed or force):
            return False
        self.missions = missions
        self.forms = {name: sorted({normalize(w) for w in m["words"]}) for name, m in missions.items()}
        self.prompt = self._build_prompt()
        self.schema = self._build_schema()
        return True

    def _describe(self, name, m):
        words = ", ".join(m["words"])
        if m.get("instant"):
            return f'- {name}: orden inmediata "{m["instant"]}", sin parámetros. Palabras: {words}.'
        return f'- {name}: acción "{m.get("actionType")}" sobre un objeto. Palabras: {words}.'

    def _build_prompt(self):
        missions = "\n".join(self._describe(n, m) for n, m in self.missions.items())
        corrected = ('1. Reconstruye la frase que el usuario dijo realmente. Las palabras mal transcritas '
                     'suelen ser del vocabulario (p. ej. "almasen" → almacén, "tornijo" → tornillo).\n'
                     if self.write_corrected else
                     '1. Piensa qué dijo realmente el usuario. Las palabras mal transcritas suelen ser del '
                     'vocabulario (p. ej. "almasen" → almacén, "tornijo" → tornillo).\n')
        fields = (['"corrected": "<frase>"'] if self.write_corrected else []) + [
            f'"mission": "{"|".join(self.missions)}|none"', '"object": ...', '"quantity": ...', '"zone": ...',
            '"confidence": <número de 0 a 1: 1 = seguro, 0.5 = dudoso>']
        return f"""Eres el intérprete de comandos de voz de un robot. Recibes la transcripción de Whisper \
en castellano, que puede tener errores fonéticos: letras cambiadas, palabras partidas, sílabas perdidas, \
artículos absorbidos u homófonos.

El robot solo entiende estas órdenes (el verbo puede llevar pronombres: "tráela", "déjalo"):
{missions}

Parámetros (solo para órdenes sobre un objeto; null en órdenes inmediatas):
- object: uno de [{", ".join(self.vocab["objects"])}], en singular. null si no se dice o no es ninguno de esos.
- quantity: cuántos objetos (número entero). "la caja" o "una caja" = 1, "dos cajas" o "2 cajas" = 2. null si object es null.
- zone: una de [{", ".join(self.vocab["zones"])}]. Al coger, de dónde; al dejar, dónde. null si no se dice o no es ninguna ("aquí" no es una zona).

Tarea:
{corrected}2. Decide la orden: {", ".join(self.missions)} o none si la frase no es ninguna de esas órdenes.
3. Extrae los parámetros. No inventes: si no estás seguro de un parámetro, pon null.
4. Da una confianza entre 0 y 1.

Responde solo con JSON: {{{", ".join(fields)}}}"""

    def _build_schema(self):
        props = {
            "mission": {"type": "string", "enum": [*self.missions, "none"]},
            "object": {"enum": self.vocab["objects"] + [None]},
            "quantity": {"type": ["integer", "null"], "minimum": 1, "maximum": 99},
            "zone": {"enum": self.vocab["zones"] + [None]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        }
        if self.write_corrected:
            props = {"corrected": {"type": "string"}, **props}
        return {"type": "object", "properties": props, "required": list(props)}

    # ---- partes deterministas ------------------------------------------------------------

    def baseline(self, text):
        """Copia exacta del comparador de app/logic.py: primera palabra completa que dispara una misión."""
        for word in normalize(text).split():
            for name, forms in self.forms.items():
                if word in forms:
                    return name
        return "none"

    def scores(self, text):
        """Cuánto suenan las primeras palabras (solas o unidas: "de ja" -> "deja") a cada misión, 0..1."""
        t = normalize(text).split()[:3]
        cands = t + ["".join(t[i:j]) for i in range(len(t)) for j in range(i + 2, len(t) + 1)]
        return {name: max((similarity(f, c) for f in forms for c in cands), default=0.0)
                for name, forms in self.forms.items()}

    def phonetic(self, text):
        """Decisión sin LLM: la misión a la que más suena la frase, o none si está demasiado reñido."""
        s = sorted(self.scores(text).items(), key=lambda kv: kv[1], reverse=True)
        if not s:
            return "none"
        if len(s) == 1 or s[0][1] - s[1][1] >= PHONETIC_MARGIN:
            return s[0][0]
        return "none"

    def sounds_like_other(self, text, mission):
        """Nombre de otra misión a la que la frase suena claramente más, o None."""
        s = self.scores(text)
        rival = max((n for n in s if n != mission), key=s.get, default=None)
        return rival if rival and s[rival] - s[mission] >= VETO_MARGIN else None

    def heard(self, text, value):
        """True si alguna palabra de `value` (4+ letras) suena a una palabra, o a dos unidas, de la frase."""
        tokens = normalize(text).split()
        cands = [t for t in tokens + [a + b for a, b in zip(tokens, tokens[1:])] if len(t) >= 4]
        keys = [w for w in normalize(value).split() if len(w) >= 4]
        return any(similarity(k, c) >= FIELD_MATCH for k in keys for c in cands)

    def phrase(self, mission, obj, quantity, zone):
        """Texto canónico de la orden para el dashboard, construido solo con los campos."""
        if mission not in self.missions:
            return "(no es una orden)"
        verb = self.missions[mission]["words"][0]
        if self.missions[mission].get("instant"):
            return verb
        parts = [verb]
        if obj is None:
            parts.append("[¿objeto?]")
        elif quantity in (None, 1):
            parts.append(f"{self.vocab['articles'].get(obj, 'el')} {obj}")
        else:
            parts.append(f"{NUMBERS[quantity] if quantity < len(NUMBERS) else quantity} {self._plural(obj)}")
        if zone is not None:
            article = self.vocab["articles"].get(zone, "la")
            if self.missions[mission].get("actionType") == "dejar":
                parts.append(f"en {article} {zone}")
            else:
                parts.append("del " + zone if article == "el" else f"de {article} {zone}")
        return " ".join(parts)

    def _plural(self, word):
        if word in self.vocab.get("plurals", {}):
            return self.vocab["plurals"][word]
        head, *rest = word.split(" ", 1)
        return " ".join([head + ("s" if head[-1] in "aeiouáéó" else "es"), *rest])

    # ---- LLM -----------------------------------------------------------------------------

    def ask_llm(self, text):
        from openai import OpenAI  # solo hace falta si se usa el LLM (extra "eval")
        if self._client is None:
            self._client = OpenAI(base_url=self.llm_url, api_key="EMPTY")
            self.model = self.model or self._client.models.list().data[0].id
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": self.prompt}, {"role": "user", "content": text}],
            temperature=0.0,
            max_tokens=192,
            response_format={"type": "json_schema", "json_schema": {"name": "command", "schema": self.schema}},
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        try:
            return json.loads(resp.choices[0].message.content)
        except (json.JSONDecodeError, TypeError):
            return {"mission": "none", "object": None, "quantity": None, "zone": None,
                    "confidence": 0.0, "parse_error": True}

    # ---- camino completo -----------------------------------------------------------------

    def interpret(self, text, use_llm=True, use_phonetic=True, use_guard=True):
        self.refresh()
        start = time.perf_counter()
        llm = self.ask_llm(text) if use_llm else {}
        llm_ms = (time.perf_counter() - start) * 1000
        return self.decide(text, llm, use_llm, use_phonetic, use_guard, llm_ms)

    def decide(self, text, llm, use_llm=True, use_phonetic=True, use_guard=True, llm_ms=0.0):
        """Combina una respuesta del LLM (desde texto o desde audio) con el comparador fonético y los vetos."""
        phon = self.phonetic(text) if use_phonetic else "none"
        llm_mission = llm.get("mission", "none")
        notes = []

        # 1. misión: el comparador fonético y el LLM se vigilan entre sí
        if use_llm and use_phonetic:
            if llm_mission != "none" and phon != "none" and llm_mission != phon:
                mission = "none"
                notes.append(f"desacuerdo: LLM={llm_mission}, fonético={phon}")
            elif llm_mission != "none":
                mission = llm_mission
            elif phon != "none" and self.scores(text)[phon] >= STRONG_MATCH:
                mission = phon
            else:
                mission = "none"
                if phon != "none":
                    notes.append(f"fonético={phon} demasiado débil sin el LLM")
        else:
            mission = llm_mission if use_llm else phon
        if use_guard and mission != "none":
            rival = self.sounds_like_other(text, mission)
            if rival:
                notes.append(f"veto: {mission} suena a {rival}")
                mission = "none"
        if use_guard and self.missions.get(mission, {}).get("instant"):
            # las órdenes inmediatas no llevan objeto: "pra esa caja" es más probable "trae" que "para"
            heard_obj = next((o for o in self.vocab["objects"] if self.heard(text, o)), None)
            if heard_obj:
                notes.append(f"veto: {mission} no lleva objeto y se oye '{heard_obj}'")
                mission = "none"

        # 2. campos (solo del LLM); se descartan si no se oyen
        fields = {k: llm.get(k) for k in ("object", "quantity", "zone")}
        if mission == "none" or self.missions.get(mission, {}).get("instant"):
            fields = dict.fromkeys(fields)
        elif use_guard:
            for k in ("object", "zone"):
                if fields[k] is not None and not self.heard(text, fields[k]):
                    notes.append(f"descartado {k}={fields[k]} (no se oye)")
                    fields[k] = None
            if fields["object"] is None:
                fields["quantity"] = None

        instant = self.missions.get(mission, {}).get("instant")
        action = "execute" if mission != "none" and (instant or fields["object"] is not None) else "repeat"
        return {
            "text": text,
            "mission": mission,
            **fields,
            "action": action,
            "phrase": self.phrase(mission, *fields.values()),
            "llm_mission": llm_mission if use_llm else None,
            "phonetic_mission": phon if use_phonetic else None,
            "corrected": llm.get("corrected"),
            "confidence": llm.get("confidence"),
            "notes": notes,
            "llm_ms": llm_ms if use_llm else 0.0,
            "parse_error": llm.get("parse_error", False),
        }


if __name__ == "__main__":
    import sys
    interp = CommandInterpreter(os.environ.get("MISSIONS_SOURCE"))
    for line in sys.argv[1:] or sys.stdin:
        print(json.dumps(interp.interpret(line.strip()), ensure_ascii=False))
