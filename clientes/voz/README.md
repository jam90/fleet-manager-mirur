# Mission Manager

FastAPI gateway: browser voice (MediaRecorder) → whisper.cpp server → decision → client.

Requires an external **whisper.cpp** `whisper-server` started with `--convert`
(so it accepts webm/ogg/mp3 directly).

## Config

```bash
cp .env.example .env   # edit WHISPER_URL / WHISPER_HEALTH_URL
```

## Run locally

```bash
uv sync
uv run uvicorn app.main:app --reload
uv run pytest   # logic tests
```

For local runs point the URLs at `http://localhost:8080/...` instead of `host.docker.internal`.

## Run with Docker

```bash
docker compose up --build
```

`whisper-check` polls `WHISPER_HEALTH_URL` and the app only starts once it succeeds.

Open http://localhost:8000 (the microphone only works on `localhost` or HTTPS).

## API

- `POST /api/voice` — multipart field `audio` → `{transcription, normalized, mission, matched_word, actionType}`
- `POST /api/missions/{mission}` — send that mission to the Fleet Manager
- `GET /api/orders/{orderId}` — order status from the Fleet Manager
- `POST /api/orders/{orderId}/cancel` — cancel it (`cancelOrder` on its robot)
- `GET /api/config` — wake/confirm words, windows and missions for the page
- all Fleet Manager endpoints take `?fleet=sim|real` (default `sim`)
- `GET /health` — `{status, whisper: ok|down}`

```bash
curl -F audio=@sample.wav http://localhost:8000/api/voice
```

## Missions

`missions.json` maps each mission to its trigger words. Matching is whole-word,
case- and accent-insensitive ("Tráelo" → `traelo`); the first trigger word spoken wins.
The file is re-read on every request and mounted into the container, so edits apply immediately.

Each entry also says which Fleet Manager mission to run:

```json
"mission_2": {"actionType": "coger", "params": {}, "words": ["trae", "traelo", "traer"]}
```

Decision logic lives in `app/logic.py`.

## Hands-free (wake word)

Tick **Manos libres** on the page: the mic stays open and a VAD
([@ricky0123/vad-web](https://github.com/ricky0123/vad), loaded from jsdelivr) cuts each phrase.

- `tonto, coge la pieza` → waits for `adelante` → sends `coger`
- `tonto` (beep, ARMADO for 5 s) … `deja la pieza` → waits for `adelante` → sends `dejar`
- `tonto, para` → cancels the last order sent from the page right away (no confirmation)
- phrases without the wake word are ignored; only one request to whisper at a time

Wake word, its variants, the confirm word and the time windows are set in `.env`.

## Missions → Fleet Manager

Set `FM_SIM_URL` (simulated fleet, `:8051`) and `FM_URL` (real robots, `:8050`) in `.env`;
the page chooses the fleet (default **Virtual**). See `docs/mir-ur-api.md`.
After a voice match the page shows a **Send** button; the order is only sent when you click it,
then the page polls its status until `FINISHED`, `FAILED` or `REJECTED`.

```bash
curl -X POST 'http://localhost:8000/api/missions/mission_2?fleet=sim'   # → FM order (check "status")
curl 'http://localhost:8000/api/orders/mm-1a2b3c4d?fleet=sim'          # → current order status
curl -X POST 'http://localhost:8000/api/orders/mm-1a2b3c4d/cancel?fleet=sim'  # → cancelOrder on its robot
```
