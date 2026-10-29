# AGENTS.md — Mission Manager

Pasarela voz → misiones de robot para la flota MiR250 (proyecto de robótica de Tknika).
El usuario habla en el navegador, un servidor whisper.cpp transcribe, esta app asocia el
texto con una misión y, tras confirmar por voz o con un clic, la envía al Fleet Manager (FM).

## Flujo

```
Navegador (app/static/index.html)
  ├─ Manos libres: micro → VAD Silero (@ricky0123/vad-web, CDN) → WAV de 16 kHz por frase
  └─ Botón: MediaRecorder (webm) → pulsar para hablar
        │  POST /api/voice
        ▼
FastAPI (app/main.py) ──► whisper.cpp  POST /inference (language=es, prompt=palabra de activación + vocabulario de comandos)
        │  decide(text) en app/logic.py → {wake, command, confirm, mission, kind, actionType}
        ▼
Máquina de estados del navegador (manos libres):
  escuchando ─"tonto"→ ARMADO (8 s) ─orden→ confirmar (10 s) ─"adelante"→ enviar
  escuchando ─"tonto, coge la caja"→ confirmar ─"adelante"→ enviar
  "tonto, para" → cancela al momento la última order (sin confirmación)
  sin palabra de activación → se ignora · orden desconocida → "no entendido"
        │  POST /api/missions/{mission}?fleet=sim|real
        ▼
REST del Fleet Manager (docs/mir-ur-api.md)  POST /api/fleet/order?wait=5, consulta /api/orders/{id}
```

## Archivos

| Ruta | Qué |
|---|---|
| `app/main.py` | Rutas: `/`, `/api/voice`, `/api/missions/{m}`, `/api/orders/{id}`, `/api/orders/{id}/cancel`, `/api/config`, `/health` |
| `app/logic.py` | `normalize`, `levenshtein`, `is_word` (activación/confirmación, distancia de edición ≤ 1 + variantes), `load_missions`, `match_mission`, `decide` |
| `app/whisper_client.py` | `transcribe()` → `/inference` de whisper; `is_healthy()` |
| `app/voice_log.py` | `save_audio`, `log_event`: cada frase (`voice`), order enviada (`order`) y cancelación (`cancel`) → `logs/voice.jsonl` + `logs/audio/` |
| `app/fm_client.py` | `send_order`, `get_order`, `instant_action` (todas reciben la `base_url` del FM) |
| `app/config.py` | pydantic-settings desde `.env`; `settings.fleet_url("sim"|"real")` |
| `app/static/index.html` | Toda la UI + máquina de estados de manos libres, VAD, codificador WAV, medidores, logs de depuración `[MM]` |
| `missions.json` | Tabla de misiones: `actionType` + `words` de disparo; las entradas con `"instant": "cancelOrder"` son órdenes de parada. Se relee en cada petición; montada en solo lectura en el contenedor |
| `tests/test_logic.py` | pytest del análisis de activación / orden / confirmación / parada |
| `tests/test_voice_log.py` | pytest del registro de voz |
| `docs/mir-ur-api.md` | Referencia de la API REST del Fleet Manager |
| `TODOS.md` | Mejoras pendientes |

## Configuración (`.env`, plantilla en `.env.example`)

`WHISPER_URL`, `WHISPER_HEALTH_URL`, `WHISPER_LANGUAGE`, `WHISPER_PROMPT`, `MISSIONS_FILE`,
`FM_URL` (flota real), `FM_SIM_URL` (flota simulada), `WAKE_WORD`, `WAKE_VARIANTS` (CSV),
`CONFIRM_WORD`, `ARM_WINDOW_S`, `CONFIRM_WINDOW_S`, `APP_PORT`, `VOICE_LOG_DIR` (vacío = apagado), `VOICE_LOG_AUDIO`.
Dentro de Docker usa `host.docker.internal` para los servicios del host (whisper :8080, FM :8050/:8051).

## Comandos

```bash
uv sync                                        # venv + dependencias
uv run pytest                                  # tests de la lógica
uv run uvicorn app.main:app --reload           # ejecución local (apunta las URLs de .env a localhost)
docker compose up -d --build --force-recreate app   # desplegar cambios de código (una reconstrucción)
docker compose up -d --force-recreate app      # aplicar solo cambios de .env
docker compose logs -f app                     # transcripciones, orders, errores
```

`whisper-check` (compose) espera a `WHISPER_HEALTH_URL`; si whisper está caído la app no arranca.

## Reglas de seguridad

- **El FM `:8050` es la flota real: los robots se mueven de verdad.** `:8051` es la flota simulada.
  La página y todos los endpoints del FM usan `fleet=sim` por defecto. Nunca enviar orders a la
  flota real salvo que el usuario lo pida explícitamente.
- Las orders solo salen tras confirmar ("adelante" o el botón Enviar). La parada ("para") es
  inmediata a propósito.
- `logs/audio/` contiene grabaciones de voz de quien hable: está en .gitignore; nunca commitearlo ni compartirlo.
  Pon `VOICE_LOG_AUDIO=false` para guardar solo el registro de texto.

## Cómo trabajar con el usuario

- Hacer solo lo que se pide; proponer extras y esperar. Confirmar antes de cambios en varios archivos.
- Una reconstrucción de Docker por cambio, con `--force-recreate` (un `up --build` a secas dejó una vez
  el contenedor viejo en marcha). No dejar nunca builds en segundo plano, servidores de prueba ni navegadores headless en marcha.
- Matar procesos por PID, no con `pkill -f <patrón>` (coincide con la propia shell del agente).
- El usuario escribe en castellano o en inglés; responder en el idioma que use.

## Problemas encontrados hasta ahora

- **El micro del navegador necesita un contexto seguro**: abrir `http://localhost:8000` desde Windows, no la IP de WSL.
- **AudioContext congelado en el Windows del usuario**: un `AudioContext` normal dice
  `running` pero `currentTime` nunca avanza (salida de audio bloqueada), así que Web Audio
  no recibe datos del micro. El VAD y el medidor usan `new AudioContext({ sinkId: { type: "none" } })`
  (Chrome/Edge ≥ 110); el pitido usa otro contexto normal y puede no sonar ahí.
- Whisper con audio corto o en silencio puede devolver `[MÚSICA]` o similar; no hace daño porque
  se exige la palabra de activación.
- Whisper oye mal algunas órdenes ("deja la caja" → "de hacha caja", "trae" → "traes"/"pra");
  ver TODOS.
- La librería del VAD (vad-web 0.0.31 + onnxruntime-web 1.22.0) se carga desde jsdelivr: el navegador necesita internet.
- Pruebas headless: el Chromium de Playwright está en caché en `~/.cache/ms-playwright`; manejarlo por
  CDP con `--use-fake-device-for-media-stream --use-file-for-fake-audio-capture=<wav>`.

## Estado (2026-10-26)

Funciona de punta a punta por voz en la flota simulada: "¡Tonto! Trae esa caja" → "Adelante"
→ order `coger` → asignada a sim-1 → FINISHED. Lo pendiente está en `TODOS.md`.
