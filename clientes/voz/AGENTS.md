# AGENTS.md — Mission Manager

Voice → robot missions gateway for the MiR250 fleet (Tknika robotics project).
The user speaks in the browser, a whisper.cpp server transcribes, this app matches the
text to a mission and, after voice/click confirmation, sends it to the Fleet Manager (FM).

## Flow

```
Browser (app/static/index.html)
  ├─ Hands-free: mic → Silero VAD (@ricky0123/vad-web, CDN) → 16 kHz WAV per phrase
  └─ Button: MediaRecorder (webm) → push-to-talk
        │  POST /api/voice
        ▼
FastAPI (app/main.py) ──► whisper.cpp  POST /inference (language=es, prompt=wake word + command vocabulary)
        │  decide(text) in app/logic.py → {wake, command, confirm, mission, kind, actionType}
        ▼
Browser state machine (hands-free):
  listening ─"tonto"→ ARMADO (8 s) ─command→ confirm (10 s) ─"adelante"→ send
  listening ─"tonto, coge la caja"→ confirm ─"adelante"→ send
  "tonto, para" → cancel last order immediately (no confirmation)
  no wake word → ignored · unknown command → "no entendido"
        │  POST /api/missions/{mission}?fleet=sim|real
        ▼
Fleet Manager REST (docs/mir-ur-api.md)  POST /api/fleet/order?wait=5, poll /api/orders/{id}
```

## Files

| Path | What |
|---|---|
| `app/main.py` | Routes: `/`, `/api/voice`, `/api/missions/{m}`, `/api/orders/{id}`, `/api/orders/{id}/cancel`, `/api/config`, `/health` |
| `app/logic.py` | `normalize`, `levenshtein`, `is_word` (wake/confirm, edit distance ≤ 1 + variants), `load_missions`, `match_mission`, `decide` |
| `app/whisper_client.py` | `transcribe()` → whisper `/inference`; `is_healthy()` |
| `app/voice_log.py` | `save_audio`, `log_event`: every phrase (`voice`), order sent (`order`) and cancel (`cancel`) → `logs/voice.jsonl` + `logs/audio/` |
| `app/fm_client.py` | `send_order`, `get_order`, `instant_action` (all take the FM `base_url`) |
| `app/config.py` | pydantic-settings from `.env`; `settings.fleet_url("sim"|"real")` |
| `app/static/index.html` | Whole UI + hands-free state machine, VAD, WAV encoder, meters, `[MM]` debug logs |
| `missions.json` | Mission table: `actionType` + trigger `words`; `"instant": "cancelOrder"` entries are stop commands. Re-read on every request, mounted read-only in the container |
| `tests/test_logic.py` | pytest for wake word / command / confirm / stop parsing |
| `tests/test_voice_log.py` | pytest for the voice log |
| `docs/mir-ur-api.md` | Fleet Manager REST API reference |
| `TODOS.md` | Pending improvements |

## Config (`.env`, template in `.env.example`)

`WHISPER_URL`, `WHISPER_HEALTH_URL`, `WHISPER_LANGUAGE`, `WHISPER_PROMPT`, `MISSIONS_FILE`,
`FM_URL` (real fleet), `FM_SIM_URL` (simulated fleet), `WAKE_WORD`, `WAKE_VARIANTS` (CSV),
`CONFIRM_WORD`, `ARM_WINDOW_S`, `CONFIRM_WINDOW_S`, `APP_PORT`, `VOICE_LOG_DIR` (empty = off), `VOICE_LOG_AUDIO`.
Inside Docker use `host.docker.internal` for services on the host (whisper :8080, FM :8050/:8051).

## Commands

```bash
uv sync                                        # venv + deps
uv run pytest                                  # logic tests
uv run uvicorn app.main:app --reload           # local run (point .env URLs at localhost)
docker compose up -d --build --force-recreate app   # deploy code changes (one rebuild)
docker compose up -d --force-recreate app      # apply .env changes only
docker compose logs -f app                     # transcriptions, orders, errors
```

`whisper-check` (compose) waits for `WHISPER_HEALTH_URL`; if whisper is down the app does not start.

## Safety rules

- **FM `:8050` is the real fleet — robots physically move.** `:8051` is the simulated fleet.
  The page and every FM endpoint default to `fleet=sim`. Never send orders to the real
  fleet unless the user explicitly asks.
- Orders only go out after confirmation ("adelante" or the Send button). Stop ("para") is
  immediate on purpose.
- `logs/audio/` holds voice recordings of whoever speaks: gitignored, never commit or share it.
  Set `VOICE_LOG_AUDIO=false` to keep only the text log.

## Working with the user

- Do only what was asked; propose extras and wait. Confirm before multi-file changes.
- One Docker rebuild per change, with `--force-recreate` (plain `up --build` once left the old
  container running). Never leave background builds, test servers or headless browsers running.
- Kill processes by PID, not `pkill -f <pattern>` (it matches the agent's own shell).
- The user replies in Spanish or English; answer in the language they use.

## Gotchas found so far

- **Browser mic needs a secure context**: open `http://localhost:8000` from Windows, not the WSL IP.
- **Frozen AudioContext on the user's Windows machine**: a normal `AudioContext` reports
  `running` but `currentTime` never advances (blocked audio output device), so Web Audio
  gets no mic data. The VAD/meter use `new AudioContext({ sinkId: { type: "none" } })`
  (Chrome/Edge ≥ 110); the beep uses a separate normal context and may be silent there.
- Whisper on short/silent audio may return `[MÚSICA]` or similar; harmless because the
  wake word is required.
- Whisper mishears some commands ("deja la caja" → "de hacha caja", "trae" → "traes"/"pra");
  see TODOS.
- The VAD library (vad-web 0.0.31 + onnxruntime-web 1.22.0) loads from jsdelivr: the browser needs internet.
- Headless testing: Playwright's Chromium is cached in `~/.cache/ms-playwright`; drive it over
  CDP with `--use-fake-device-for-media-stream --use-file-for-fake-audio-capture=<wav>`.

## Status (2026-10-26)

Working end to end by voice on the simulated fleet: "¡Tonto! Trae esa caja" → "Adelante"
→ order `coger` → assigned to sim-1 → FINISHED. Pending work is in `TODOS.md`.
