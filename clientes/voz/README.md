# Pasarela de voz (`clientes/voz`)

Pasarela FastAPI: voz del navegador (MediaRecorder) → servidor whisper.cpp → decisión → Fleet Manager.
Antes era el repo `mission-manager`. Es un cliente del FM por su API REST; los modelos se sirven desde el
repo `inferencia`. Cómo funciona, cómo reproducirlo y cifras: [`docs/PROPUESTA_DETECCION_VOZ.md`](docs/PROPUESTA_DETECCION_VOZ.md).

Necesita un `whisper-server` de **whisper.cpp** externo, arrancado con `--convert`
(para que acepte webm/ogg/mp3 directamente).

## Configuración

```bash
cp .env.example .env   # editar WHISPER_URL / WHISPER_HEALTH_URL
```

## Ejecución local

```bash
uv sync
uv run uvicorn app.main:app --reload
uv run pytest   # tests de la lógica
```

En local, apunta las URLs a `http://localhost:8080/...` en vez de a `host.docker.internal`.

## Ejecución con Docker

```bash
docker compose up --build
```

`whisper-check` consulta `WHISPER_HEALTH_URL` y la app solo arranca cuando responde.

Abre http://localhost:8000 (el micrófono solo funciona en `localhost` o con HTTPS).

## API

- `POST /api/voice`: campo multipart `audio` → `{transcription, normalized, mission, matched_word, actionType}`
- `POST /api/missions/{mission}`: envía esa misión al Fleet Manager
- `GET /api/orders/{orderId}`: estado de la order en el Fleet Manager
- `POST /api/orders/{orderId}/cancel`: la cancela (`cancelOrder` en su robot)
- `GET /api/config`: palabras de activación y confirmación, ventanas y misiones para la página
- todos los endpoints del Fleet Manager aceptan `?fleet=sim|real` (por defecto `sim`)
- `GET /health`: `{status, whisper: ok|down}`

```bash
curl -F audio=@sample.wav http://localhost:8000/api/voice
```

## Misiones

`missions.json` asocia cada misión con sus palabras de disparo. La coincidencia es por palabra
completa, sin distinguir mayúsculas ni tildes ("Tráelo" → `traelo`); gana la primera palabra de
disparo que se diga. El archivo se relee en cada petición y está montado en el contenedor, así que
los cambios se aplican al momento.

Cada entrada dice también qué misión del Fleet Manager se ejecuta:

```json
"mission_2": {"actionType": "coger", "params": {}, "words": ["trae", "traelo", "traer"]}
```

La lógica de decisión está en `app/logic.py`.

## Evaluación (`eval/`)

```bash
uv sync --extra eval
uv run python -m eval.bench_correction      # texto con errores de Whisper (necesita el LLM en :8001)
uv run python -m eval.make_audio            # genera el audio de prueba (descarga voces Piper la 1.ª vez)
uv run python -m eval.bench_audio           # audio → whisper → decisión
```

## Manos libres (palabra de activación)

Marca **Manos libres** en la página: el micro se queda abierto y un VAD
([@ricky0123/vad-web](https://github.com/ricky0123/vad), cargado desde jsdelivr) corta cada frase.

- `tonto, coge la pieza` → espera `adelante` → envía `coger`
- `tonto` (pitido, ARMADO durante 5 s) … `deja la pieza` → espera `adelante` → envía `dejar`
- `tonto, para` → cancela al momento la última order enviada desde la página (sin confirmación)
- las frases sin palabra de activación se ignoran; solo hay una petición a whisper a la vez

La palabra de activación, sus variantes, la palabra de confirmación y las ventanas de tiempo se
configuran en `.env`.

## Misiones → Fleet Manager

Configura `FM_SIM_URL` (flota simulada, `:8051`) y `FM_URL` (robots reales, `:8050`) en `.env`;
la página elige la flota (por defecto **Virtual**). Ver `docs/mir-ur-api.md`.
Tras reconocer una orden por voz, la página muestra un botón **Enviar**: la order solo sale al
pulsarlo, y luego la página consulta su estado hasta `FINISHED`, `FAILED` o `REJECTED`.

```bash
curl -X POST 'http://localhost:8000/api/missions/mission_2?fleet=sim'   # → order del FM (mira "status")
curl 'http://localhost:8000/api/orders/mm-1a2b3c4d?fleet=sim'          # → estado actual de la order
curl -X POST 'http://localhost:8000/api/orders/mm-1a2b3c4d/cancel?fleet=sim'  # → cancelOrder en su robot
```
