# AGENTS.md — memoria corta del proyecto

Fleet Manager de robots móviles (2× MiR250) que habla **MQTT VDA 5050 v3.0.0**
con el mundo y **REST** con cada robot vía drivers enchufables. Sin ROS 2,
sin MiR Fleet, sin Open-RMF. Código didáctico (FP robótica): legible,
comentarios que explican el *porqué*.

## Layout

- `run_fm.py` — entrada (`--config`, `--robot`, `--period`, `--web-port 0`=sin web)
- `fm/config.py` — `.env` + `config/fleet.yaml` → dataclasses genéricas
- `fm/robot.py`, `fm/orders.py`, `fm/assigner.py`, `fm/fleet.py` — runtime,
  tracker `orderUpdateId`, asignador puro, Dispatcher MQTT
- `fm/mqtt_bus.py` — paho; **sin broker no arranca** (`TimeoutError`)
- `fm/charge.py` — auto-carga bajo `battery_floor`, no aborta orders
- `fm/adapters/<marca>/` — drivers (`mir`, `sim`); el core no importa MiR
  (`grep -rn mir fm/*.py fm/vda5050` debe dar vacío salvo `mir_client.py`)
- `fm/vda5050/` — header (topic≡header), state, order, schemas, factsheet
- `fm/web/` — UI FastAPI `:8050` (operario + Crear/Configurador/Sincronización) y
  API REST para integradores (`docs/api-rest.md`); es un cliente VDA más (publica al broker)
- `config/fleet.yaml` + `catalog/` (datos de la instalación, **fuera de git**: decisión 135;
  `FM_CONFIG`/`FM_CATALOG` en `.env` o `--config`/`--catalog`) / `config/fleet-sim.yaml`
  (demo sin hardware) / `config/fleet.example.yaml` (punto de partida, lo usan los tests)
- `scripts/` — `read_status`, `list_missions`, `send_order`, `send_fleet_order`,
  `send_instant_action`, `read_vda_state` (usan el mismo header que el servicio)
- `clientes/` — pasarelas de IA hacia el FM, cada una un servicio aparte con su
  propio `AGENTS.md`, venv (uv) y tests. Hoy `clientes/voz/` (navegador → whisper
  → `POST /api/fleet/order`); en el futuro visión. **`fm/` nunca importa
  `clientes/`** (`tests/test_independencia.py`): son clientes del FM por REST/MQTT,
  como un MES. Los modelos (whisper, LLM) no viven aquí sino en el repo `inferencia`.

## Entorno y ejecución

- Robots: `mir-1` 192.168.15.5, `mir-2` 192.168.15.15; token en `.env`
  (`AUTH_HEADER`/`MIR_AUTH…`), nunca en git (`.env.example` sí se commitea).
- Venv: `env -u PYTHONPATH .venv/bin/python …` (el WSL mete ROS Jazzy en el
  site-packages del sistema; sin esto peta).
- Tests: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q`
  (sin red; no incluyen `clientes/`). Contrato de drivers: `tests/adapters/test_contract.py`.
- Local clásico: Mosquitto en `localhost:1883` sin auth + `run_fm.py`
  (web `:8050`); `--config config/fleet-sim.yaml` sin robots.
- Compose: `docker compose up --build` (mosquitto + flota real, web `:8050`);
  `docker compose --profile sim up --build fleet-sim` (demo, web `:8051`).
  Voz: `docker compose --profile sim --profile voz up --build fleet-sim voz`
  (pasarela en `:8000`; necesita el whisper del repo `inferencia` en el host `:8080`).
  Tests de la voz aparte: `cd clientes/voz && uv run pytest` (16).
  Dentro de compose el broker es `MQTT_HOST=mosquitto` (gana al `.env`
  porque `load_dotenv()` no pisa variables existentes); desde el host todo
  sigue en `localhost:1883`. Ver `docker-compose.yml`, `Dockerfile.fm`,
  `mosquitto/config/mosquitto.conf`.
- Parar el FM con `kill -INT <pid>` (Ctrl-C); nunca `pkill -f` en la misma
  línea (se mata a sí mismo). Cierre limpio → `connection: OFFLINE`;
  muerte brusca → broker emite `CONNECTION_BROKEN`.

## Convenciones (obligatorias)

- Castellano en código, comentarios, docs y commits. No `git commit/push`
  sin pedirlo explícitamente.
- Bitácora viva en `docs/avance.md` (fecha + decisión + motivo, al decidir).
  Brief completo: `CLAUDE.md`. Contrato de integración: `README.md`.
- Nunca hardcodear GUIDs (missions/positions/mapas cambian por robot):
  nombres lógicos en `fleet.yaml`, resolución a GUID por robot al arranque.
- Cliente REST con patrón `<recurso>_<verbo>` calcado del path
  (`status_get`, `mission_queue_post`, …).
- Traducción pura y testeable (`adapters/mir/translate.py`,
  `state_builder.py`, `assigner.py` sin I/O). Un hilo principal a 1 Hz;
  paho solo encola en `bus.inbox`. `driver.poll()` nunca lanza (None/False).
- Antes de mover hardware real (misiones), **preguntar al usuario**.

## Gotchas MiR verificados

- `POST /mission_queue`: parámetros como `{"id": "<input_name de Blockly>",
  "value": …}` — con el id interno da `400 invalid_input_data`.
- `orientation` en grados → VDA quiere radianes. GUIDs distintos por robot.
- Mission de carga con **porcentaje objetivo** (70 %), nunca "hasta nueva
  mission" (deadlock: `busy` eterno). Histéresis: `battery_min` (25) >
  `battery_floor` (20).
- Topics `vda5050/v3/<manufacturer>/<serial>/…`; `fleet/*` bajo
  `imperial_fleet` con `serialNumber: "fleet"` (extensión propia).
  Header ≡ topic siempre; `headerId` monótono por topic.
