# BRIEF — Fleet Manager MiR250 "simple" (VDA 5050)

> Documento de arranque para una instancia de Claude Code en un entorno nuevo.
> Léelo entero antes de escribir código. Contiene el objetivo, el alcance, el
> contrato MQTT/VDA 5050, las reglas de negocio, las particularidades del MiR250
> que ya nos han mordido en un proyecto anterior, y un plan por hitos.
> Cópialo como `CLAUDE.md` en la raíz del nuevo repo (o enlázalo desde él).

---

## 0. Cómo trabajar (convenciones para Claude)

- **Idioma:** código, comentarios, docs y commits en **castellano** (nombres de
  variables/funciones en inglés o castellano, pero consistentes). El usuario es
  profesor de robótica en FP; el código se usará también como material didáctico:
  prioriza legibilidad y comentarios que expliquen *por qué*, no *qué*.
- **No hagas `git commit` ni `git push`** sin que el usuario lo pida
  explícitamente. Puedes preparar el stage y proponer el mensaje.
- **Bitácora:** mantén `docs/avance.md` con las decisiones según se toman (fecha
  absoluta + decisión + motivo). No esperes a cerrar un hito. Omite caminos
  muertos que no dejaron código.
- **Secretos fuera de git:** `.env` en `.gitignore`; commitea `.env.example`.
- **Nunca hardcodees GUIDs** de missions/positions/mapas: son distintos en cada
  robot aunque el nombre coincida (§9). Trabaja con **nombres lógicos** y
  resuélvelos por robot al arranque.
- **Nombres del cliente REST:** usa el patrón `<recurso>_<verbo>` calcado del
  path (`status_get`, `missions_get`, `mission_queue_post`,
  `mission_queue_id_get`, `mission_queue_id_delete`, `positions_get`,
  `missions_mission_id_actions_get`, `status_put`). Facilita grep-ear el endpoint.
- **Sin Open-RMF, sin ROS 2, sin MiR Fleet.** Es un servicio Python autónomo
  que habla REST con los robots y MQTT con el mundo.
- Cuando haya que probar contra hardware, **pregunta al usuario** antes de
  lanzar cualquier mission: los robots se mueven físicamente.

---

## 1. Objetivo

Servicio Python ("Fleet Manager", FM) que gestiona una flota de **MiR250**
(2 unidades inicialmente, diseñar para N) y que:

1. **Recibe órdenes por MQTT siguiendo VDA 5050 v3.0.0** (mensajes `order`,
   `instantActions`; publica `state` y `connection`).
2. **Elige qué robot ejecuta cada misión** atendiendo únicamente a:
   - que el robot **no esté ocupado** (sin mission viva en su cola), y
   - que tenga **batería suficiente** (≥ `battery_min`, configurable).
3. **Manda al robot a cargar automáticamente** cuando su batería baja del
   **20 %** (`battery_floor`, configurable).
4. Traduce cada orden a una **mission ya creada en la web del MiR** (Blockly),
   pasándole parámetros (positions, valores numéricos) vía REST.

### Lo que NO hace (alcance v1, no lo implementes salvo que te lo pidan)

- Coordinación de tráfico entre robots (se evitan con su propio LIDAR).
- Cola interna de órdenes: si ningún robot puede aceptarla, se **rechaza** con
  motivo. El sistema aguas arriba reencola.
- Navegación por grafo `nodes`/`edges` del VDA 5050: el MiR navega solo con sus
  missions. Los `nodes` de la order son **un único nodo lógico** con `actions`.
- Cambio de herramienta, catálogo de piezas, PLC (eso era del proyecto anterior).
- Persistencia: el estado vive en RAM y en el robot.
- `factsheet` / `visualization` VDA 5050 (opcionales, se añaden si se piden).

---

## 2. Stack y dependencias

- Python **≥ 3.11**, venv propio.
- `paho-mqtt>=2.0` (usar `CallbackAPIVersion.VERSION2`), `requests>=2.31`,
  `PyYAML>=6.0`, `python-dotenv>=1.0`.
- Para tests: `pytest`; para validar VDA 5050: `jsonschema` (§10).
- Broker de desarrollo: Mosquitto local sin auth (`localhost:1883`).
  Herramientas: `mosquitto_sub -t 'vda5050/#' -v`, `mosquitto_pub`.

---

## 3. Red y credenciales MiR250

| Concepto | Valor |
|---|---|
| Base URL REST | `http://<ip>/api/v2.0.0/` |
| Headers | `Content-Type: application/json`, `Accept-Language: en_US`, `Authorization: Basic <token>` |
| Token | `base64("distributor:" + sha256_hex(<password>))` — o copiarlo de la web del MiR: Help → API → Authorize |
| IPs | Las dará el usuario. En el proyecto anterior: `192.168.15.5` y `192.168.15.15`. **Preguntar.** |

`.env.example`:

```ini
MIR_AUTH=Basic <token_base64>
MQTT_HOST=localhost
MQTT_PORT=1883
```

Un único token vale para todos los MiR si comparten password (caso habitual).
Si no, permitir `MIR_AUTH_<serial>` como override.

---

## 4. Arquitectura

> Actualizado el 2026-09-17 tras la refactorización a drivers enchufables
> (`docs/plan-drivers.md`, decisiones 22–33 de `docs/avance.md`). El core no
> sabe nada de MiR: cada marca es un driver en `fm/adapters/<marca>/`.

```
                         MQTT (Mosquitto)
  Sistema aguas arriba ──────────────────────────► vda5050/v3/imperial_fleet/fleet/order
  (ERP/MES/SCADA/UI)   ◄────────────────────────── vda5050/v3/imperial_fleet/fleet/order_response
                       ◄────────────────────────── vda5050/v3/<manufacturer>/<serial>/state (1 Hz)
                       ◄────────────────────────── vda5050/v3/<manufacturer>/<serial>/connection (retained)
                       ──────────────────────────► vda5050/v3/<manufacturer>/<serial>/order
                       ──────────────────────────► vda5050/v3/<manufacturer>/<serial>/instantActions
                                    │
                             ┌──────▼───────┐
                             │ Fleet Manager│  bucle 1 Hz por robot:
                             │   core       │  driver.poll() → Telemetry → state VDA
                             └──┬────────┬──┘  driver.job_status() → actionStates
                     RobotDriver│        │RobotDriver   (Protocol, fm/adapters/base.py)
                          ┌─────▼──┐  ┌──▼─────┐
                          │MirDriver│ │SimDriver│ ...  (una carpeta por marca)
                          └────┬───┘  └────────┘
                          REST │
                          ┌────▼───┐
                          │ MiR250 │
                          └────────┘
```

Frontera core ↔ marca (`fm/adapters/base.py`): el core solo ve `Telemetry`
(snapshot normalizado), `Job` (lo que el driver ejecutará para una `Action`)
y `JobStatus` (= `actionStatus` VDA). Un driver implementa `connect`, `poll`,
`translate`, `execute`, `job_status`, `cancel`, `charge_job`, `extra_state`.
`tests/adapters/test_contract.py` es la definición ejecutable del contrato.

### Estructura de código (real)

```
fleet-manager-mirur/
├── CLAUDE.md                      ← este brief
├── README.md                      ← contrato de integración y guía de uso
├── run_fm.py                      ← punto de entrada (args: --period, --robot, --config)
├── config/fleet.yaml              ← flota real; config/fleet-sim.yaml ← flota simulada
├── docs/avance.md                 ← bitácora de decisiones; docs/plan-drivers.md ← plan drivers
├── scripts/                       ← utilidades de prueba (§12); son herramientas MiR
├── tests/                         ← pytest sin red; tests/adapters/ ← drivers y contrato
└── fm/
    ├── config.py                  ← .env + fleet.yaml → parte GENÉRICA (driver, battery_min, action_types, raw)
    ├── robot.py                   ← Robot = driver + OrderTracker + última Telemetry
    ├── orders.py                  ← pick_action, OrderTracker (ciclo orderUpdateId, job_status → actionStates)
    ├── assigner.py                ← función pura: actionType + snapshots → robot | rechazo
    ├── fleet.py                   ← Dispatcher: <serial>/order, fleet/order → order_response
    ├── mqtt_bus.py                ← paho: publish, LWT por robot, inbox; manufacturer por serial
    ├── mir_client.py              ← shim → adapters/mir/client.py (lo usan scripts/)
    ├── adapters/
    │   ├── __init__.py            ← registro DRIVERS + make_driver()
    │   ├── base.py                ← Telemetry, Job, JobStatus, RobotDriver (Protocol)
    │   ├── mir/                   ← client.py (REST), translate.py (puro), driver.py, config.py
    │   └── sim/                   ← driver simulado sin hardware
    └── vda5050/
        ├── header.py              ← topic ≡ header (§5.1), timestamp, headerId
        ├── state.py               ← dataclasses State, Error, Info…; errorTypes
        ├── state_builder.py       ← StateOverlay + to_vda_state(header, Telemetry, overlay)
        ├── order.py               ← dataclasses Order, Node, Action; parse_order
        └── schemas.py             ← validación contra los JSON Schema oficiales
```

Principios:

- **El core no importa ninguna marca.** `grep -rn "mir" fm/*.py fm/vda5050`
  debe seguir sin resultados (salvo el shim `mir_client.py`). Lo que una
  marca necesita saber va en `fm/adapters/<marca>/`; lo que el core necesita
  de cualquier marca va en `fm/adapters/base.py`.
- **Traducción pura y testeable.** `adapters/mir/translate.py`,
  `vda5050/state_builder.py` y `assigner.py` no hacen I/O; reciben índices
  precalculados y snapshots.
- **Un hilo principal** con bucle a 1 Hz; paho solo encola en `bus.inbox`
  desde su hilo y el principal drena al inicio de cada tick (decisión 14).
  La única otra concurrencia es `Robot.poll()` (red de UN robot) en un hilo
  por robot, con espera acotada (decisión 42): el hilo solo toca el estado de
  su robot, y el principal no despacha orders a un robot cuyo poll no ha
  vuelto (está inalcanzable → `last_telemetry = None` → rechazo sin red).
  Sin locks.
- **Fallos de red tolerantes:** `driver.poll()`/`connect()`/`job_status()`
  nunca lanzan; devuelven None/False y el core reintenta al tick siguiente
  publicando `state` con `ROBOT_UNREACHABLE`.

---

## 5. Contrato MQTT — VDA 5050 v3.0.0

Convención de topics del estándar: `<interfaceName>/<majorVersion>/<manufacturer>/<serialNumber>/<topic>`

- `interfaceName = "vda5050"`, `majorVersion = "v3"`.
- `manufacturer` = **el del robot** (§6.2 de la norma): lo declara su driver
  (`MiR`, `SIM`, …) o se fuerza con `robots.<serial>.manufacturer`
  (decisión 30). El FM se suscribe a `vda5050/v3/+/+/order` e ignora pares
  `(manufacturer, serial)` que no estén configurados.
- `serialNumber` = nombre lógico del robot en `fleet.yaml` (`mir-1`, `mir-2`).
- Pseudo-serial **`fleet`** para el topic de asignación (extensión propia, no VDA).
  Como `fleet` no es un robot, su segmento `manufacturer` es el nombre de
  flota `mqtt.fleet_manufacturer` = **`imperial_fleet`**.

| Topic | Sentido | QoS | Retained |
|---|---|---|---|
| `vda5050/v3/MiR/<serial>/state` | FM → bus | 0 | no |
| `vda5050/v3/MiR/<serial>/connection` | FM → bus | 1 | **sí** + Last Will |
| `vda5050/v3/MiR/<serial>/order` | bus → FM | 1 | no |
| `vda5050/v3/MiR/<serial>/instantActions` | bus → FM | 1 | no |
| `vda5050/v3/imperial_fleet/fleet/order` | bus → FM | 1 | no |
| `vda5050/v3/imperial_fleet/fleet/order_response` | FM → bus | 1 | no |

### 5.1. Header común (todos los mensajes)

```json
{ "headerId": 1, "timestamp": "2026-09-16T10:30:00.123Z",
  "version": "3.0.0", "manufacturer": "MiR", "serialNumber": "mir-1" }
```

- **Topic ≡ header, siempre.** `manufacturer` y `serialNumber` del payload
  deben coincidir carácter a carácter con los segmentos del topic
  (`vda5050/v3/<manufacturer>/<serialNumber>/…`). Para los topics `fleet/*`
  el header es `manufacturer: "MiR", serialNumber: "fleet"` — nada de
  inventar otro fabricante para el robot virtual. El FM **valida esto en
  todo mensaje entrante** (`order`, `instantActions`, `fleet/order`) y rechaza
  con `validationError` si no cuadra; y lo cumple en todo lo que publica
  (`state`, `connection`, `order_response`). Centralizar en `vda5050/header.py`.
  (En el FM anterior los scripts de prueba enviaban `manufacturer:
  "FleetMaster"` y el servicio no lo comprobaba — es el tipo de bug sutil que
  aparece el día que alguien filtra por header en vez de por topic.)
- `serialNumber` **nunca vacío ni ausente**, tampoco en respuestas de rechazo:
  es campo obligatorio del header.
- `headerId`: entero **monótono por topic y por robot**, arranca en 1 en cada
  arranque del FM. Con `try/except` no interrumpir el contador.
- `timestamp`: ISO 8601 UTC con milisegundos y sufijo `Z`:
  `datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")`.

### 5.2. `state` (FM → bus, 1 Hz por robot)

Nombres **v3** (cambian respecto a v2): `mobileRobotPosition` (antes
`agvPosition`), `powerSupply` (antes `batteryState`).

```json
{
  "headerId": 742, "timestamp": "…", "version": "3.0.0",
  "manufacturer": "MiR", "serialNumber": "mir-2",
  "orderId": "fleet-250ef207", "orderUpdateId": 0,
  "lastNodeId": "", "lastNodeSequenceId": 0,
  "driving": true, "paused": false, "newBaseRequest": false,
  "operatingMode": "AUTOMATIC",
  "mobileRobotPosition": { "x": 29.80, "y": 7.96, "theta": -0.014,
                           "mapId": "<map-guid>", "localized": true },
  "powerSupply": { "stateOfCharge": 54.9, "charging": false },
  "nodeStates": [], "edgeStates": [],
  "actionStates": [ { "actionId": "act-1", "actionType": "ir_a", "actionStatus": "RUNNING" } ],
  "instantActionStates": [], "zoneActionStates": [],
  "errors": [], "information": [ { "infoType": "MISSION", "infoLevel": "INFO",
                                   "infoDescription": "Ir a H2D1" } ],
  "maps": [], "zoneSets": [], "zoneRequests": [], "edgeRequests": []
}
```

Mapeo desde `GET /status` del MiR:

| MiR `/status` | VDA `state` |
|---|---|
| `state_id` 3 Ready / 4 Pause / 5 Executing / 11 Manual / 12 Error | `driving = (5)`, `paused = (4)`, `operatingMode = MANUAL si 11, si no AUTOMATIC`, `errors[]` si 12 |
| `position.{x,y,orientation}` (orientation en **grados**) | `mobileRobotPosition` con `theta` en **radianes** `[-π, π]` |
| `battery_percentage` | `powerSupply.stateOfCharge` |
| `map_id` | `mobileRobotPosition.mapId` (GUID distinto por robot) |
| `mission_text` | `information[]` |
| `errors[]` del MiR | `errors[]` con `errorType = code`, `errorLevel = "URGENT"` |

`powerSupply.charging`: el proyecto anterior lo dejó siempre `false`. Aquí es
importante (auto-carga). Determínalo así, en este orden de preferencia:
(a) la mission de carga posteada por el FM está `Executing`; (b) `/status`
del MiR: campo `battery_time_remaining` creciendo / `mode_id`; (c) probar
con robot real en el dock y anotar en `docs/avance.md` qué campo lo delata.
Marca `charging` como pendiente de validación hasta hacerlo.

`actionStates[].actionStatus` ∈ `WAITING | INITIALIZING | RUNNING | PAUSED |
FINISHED | FAILED`. Granularidad gruesa: la mission MiR no da estado por
acción, así que toda la order comparte fase (RUNNING mientras `Executing`,
FINISHED con `Done`, FAILED con `Aborted`/`Cancelled`).

### 5.3. `connection` (FM → bus, retained, con Last Will)

```json
{ "headerId": 1, "timestamp": "…", "version": "3.0.0", "manufacturer": "MiR",
  "serialNumber": "mir-2", "connectionState": "ONLINE" }
```

- `will_set(...)` con `CONNECTION_BROKEN` **antes** de `connect()`, QoS 1, retained.
- Al conectar: publicar `ONLINE`. Al cerrar limpio (Ctrl-C / SIGTERM): `OFFLINE`.
- Refleja FM ↔ broker, **no** FM ↔ MiR. Una caída REST va a `state.errors`.

### 5.4. `order` (bus → FM) — subset aceptado

```json
{
  "headerId": 1, "timestamp": "…", "version": "3.0.0",
  "manufacturer": "MiR", "serialNumber": "mir-2",
  "orderId": "ord-abc123", "orderUpdateId": 0,
  "nodes": [
    { "nodeId": "H2D1", "sequenceId": 0, "released": true,
      "actions": [
        { "actionType": "ir_a", "actionId": "act-1", "blockingType": "HARD",
          "actionParameters": [ { "key": "target_pos", "value": "H2D1-VL" } ] }
      ] }
  ],
  "edges": []
}
```

Reglas (§6.1.4 del estándar, obligatorias para poder decir "cumple VDA 5050"):

- **Header ≡ topic** (§5.1): `serialNumber` debe ser el del topic → si no, `validationError`.
- **Idempotencia:** mismo `(orderId, orderUpdateId)` que el activo → descartar en silencio.
- **Regresión:** mismo `orderId` con `orderUpdateId` menor → `validationError` en `state.errors`.
- **Actualización:** mismo `orderId` con `orderUpdateId` mayor → aceptar y reemplazar
  (v1: solo validar el caso `orderUpdateId = 0`; documentar el resto como no probado).
- **Nueva order con otro `orderId` mientras hay una activa:** el estándar lo
  permite si la anterior ha terminado; si sigue viva, rechazar con `orderError`
  (aquí no hay "cola" por robot).
- Solo se ejecuta la **primera action del primer nodo `released`**. Más actions/nodos
  → `validationError` explicando el límite (mejor rechazar que ejecutar a medias).
- `actionType` no mapeado para ese robot → `noRouteError`.
- Parámetro obligatorio ausente → `validationError`.

### 5.5. `instantActions` (bus → FM)

> Implementado el 2026-09-18 (`fm/instant.py`), probado con mir-2 real. El
> `Protocol` gana `pause()`/`resume()`; `cancelOrder` usa `driver.cancel()`.

```json
{ "headerId": 1, "timestamp": "…", "version": "3.0.0", "manufacturer": "MiR",
  "serialNumber": "mir-2",
  "actions": [ { "actionType": "cancelOrder", "actionId": "ia-1",
                 "blockingType": "HARD", "actionParameters": [] } ] }
```

| `actionType` (slug literal VDA) | MiR REST |
|---|---|
| `startPause` | `PUT /status {"state_id": 4}` |
| `stopPause` | `PUT /status {"state_id": 3}` |
| `cancelOrder` | `DELETE /mission_queue/<queue_id>` de la order activa + cerrar estado interno con `orderError` en `state.errors` |
| `stateRequest` | publicar un `state` inmediatamente (fuera del tick) |
| `factsheetRequest` | opcional; si se implementa, publicar `factsheet` retained |

Publicar el resultado en `state.instantActionStates[]` (`actionStatus`
FINISHED/FAILED). `cancelOrder` sin order activa → FAILED + log, no-op.

### 5.6. `fleet/order` (bus → FM) — **topic principal**

Mismo esquema que §5.4 con header `manufacturer: "imperial_fleet", serialNumber: "fleet"`
(coherente con el topic, §5.1). El FM elige robot (§6), postea la mission y
**reescribe `state.orderId`** del robot elegido.

### 5.7. `fleet/order_response` (FM → bus) — veredicto del asignador

El emisor es el robot virtual `fleet`, así que el header lleva
`serialNumber: "fleet"` **en ambos casos**; el robot elegido va en un campo
propio `assignedSerial` (no se reutiliza `serialNumber` del header para eso).

```json
{ "headerId": 12, "timestamp": "…", "version": "3.0.0",
  "manufacturer": "imperial_fleet", "serialNumber": "fleet",
  "orderId": "fleet-dc09ef50", "orderUpdateId": 0,
  "status": "ASSIGNED", "assignedSerial": "mir-2", "description": "asignado a mir-2" }
```

```json
{ "headerId": 13, "timestamp": "…", "version": "3.0.0",
  "manufacturer": "imperial_fleet", "serialNumber": "fleet",
  "orderId": "fleet-9001", "orderUpdateId": 0,
  "status": "REJECTED", "errorType": "orderError",
  "errorDescription": "ningún robot puede atender la order: mir-1: ocupado; mir-2: batería 17.0% < 20%" }
```

Separación importante: `order_response` es el **ACK/rechazo inmediato** del
asignador. Los errores de **ejecución** posteriores (mission `Aborted`) van a
`state.errors[]` del robot con `errorReferences: [{referenceKey: "orderId", …}]`.

### 5.8. Tipos de error VDA 5050 que emite el FM

| `errorType` | Cuándo |
|---|---|
| `validationError` | JSON malformado, header ≠ topic, `orderUpdateId` regresivo, parámetro obligatorio ausente, más de una action |
| `noRouteError` | `actionType` no mapeado, position inexistente o fuera de allowlist |
| `orderError` | asignador sin candidatos (ocupado/batería), mission `Aborted`/`Cancelled`, `cancelOrder` aplicado |

---

## 6. Asignador (`assigner.py`) — función pura

Entrada: `Order`, `dict[serial → RobotSnapshot(serial, battery, busy, charging, position)]`,
`FleetConfig`. Salida: `AssignResult(serial | None, reason, error_type, rejections)`.

Filtros en orden, para cada robot (iterar `sorted(serials)`):

1. Soporta el `actionType` (existe en `robots.<serial>.actions` de `fleet.yaml`) — si no, descarte.
2. `battery ≥ battery_min` del robot — si no, descarte con `"batería X% < mínimo Y%"`.
3. No `busy` — si no, descarte `"ocupado"`. **`busy` = tiene una mission viva
   posteada por el FM (order o carga) en `Pending`/`Executing`.**
4. (Opcional, flag `prefer_not_charging`) si está cargando y hay otro
   candidato libre, preferir el otro.

Ranking entre supervivientes: **mayor batería primero**, desempate alfabético.
Deja un hueco claro (comentario + parámetro `position`) para sustituirlo por
"menor distancia al destino" más adelante.

Sin candidatos → `serial=None, error_type="orderError"`, `reason` con el
detalle por robot (se publica tal cual en `errorDescription`).

**Histéresis batería:** `battery_min` (aceptar trabajo) debe ser **mayor** que
`battery_floor` (ir a cargar), p.ej. 25 % vs 20 %, para que un robot recién
salido del suelo no reciba trabajo y vuelva a caer inmediatamente. Documentarlo
en `fleet.yaml`.

---

## 7. Auto-carga (`charge.py`)

> Implementado el 2026-09-18 como `fm/charge.py::ChargeGuard(serial, cfg)`,
> genérico sobre el driver: `tick(driver, telemetry)` cada tick, `active`
> (cuenta como busy), `charging`, `decorate(overlay)`. El job lo da
> `driver.charge_job()`; en MiR es `drivers.mir.charge_mission`. La mission
> real `Carga en estación MIRUR` carga hasta el 70 % (`charge_until_new_mission:
> false`) y sale del dock: sin riesgo de deadlock. Lo de abajo es el diseño
> original en términos REST; la semántica se mantiene.

Independiente del bus MQTT — es un mínimo de seguridad local que se evalúa
**en cada tick**, después de leer `/status`:

```python
@dataclass
class ChargeGuard:
    mission_guid: str | None      # GUID de la mission de carga EN ESTE robot; None = desactivado
    floor: float                  # % (20 por defecto)
    priority: int = 10            # prioridad alta en mission_queue
    active_queue_id: int | None = None   # latch: carga posteada y viva
    cooldown_until: float = 0.0   # evita re-postear en bucle si aborta
```

Lógica de `enforce_battery_floor(serial, client, soc, guard)`:

1. Si `active_queue_id` no es `None`: `GET /mission_queue/<id>`.
   `Pending`/`Executing` → return (sigue viva). `Done`/`Aborted`/`Cancelled` →
   liberar latch (y si fue `Aborted`, poner cooldown de ~60 s).
2. Si `soc ≥ floor` → return.
3. Si en cooldown → return.
4. `POST /mission_queue {mission_id, priority}` **sin parámetros** (el dock va
   embebido en la mission). Guardar `active_queue_id`.

Decisiones que ya están tomadas (no re-discutir):

- **No aborta nada.** Si el robot está ejecutando una order, la carga se encola
  detrás y arranca al terminar. El filtro `battery_min` del asignador impide
  que le entren orders nuevas mientras tanto.
- La mission de carga **cuenta como `busy`** para el asignador mientras esté viva.
- Mientras hay carga del FM activa, `state.information[]` lleva
  `{infoType: "AUTO_CHARGE", infoLevel: "INFO", …}`.
- Umbral único a nivel flota (`auto_charge.battery_floor`); si algún día hace
  falta por robot, mover a `capabilities`.

**Trampa a evitar (deadlock de carga):** la action *Charging* de MiR se puede
configurar "cargar hasta que llegue una nueva mission". Con ese modo la mission
**nunca pasa a `Done`**, el FM la ve `Executing` → `busy` → nunca le asigna
nada → nunca sale del dock. Solución: crear la mission de carga en la web del
MiR con **porcentaje objetivo** (p.ej. 80 %) o **tiempo mínimo**, para que
termine `Done` sola. Alternativa (si el usuario prefiere el modo "hasta nueva
mission"): tratar como no-busy a un robot cuya única mission viva es la de
carga y `soc ≥ battery_resume` (p.ej. 60 %) — postear una order entonces saca
al robot del dock. Elegir una y anotarlo en `docs/avance.md`.

---

## 8. `config/fleet.yaml`

> Formato vigente desde 2026-09-17 (fase 3 de drivers). El core solo lee
> `driver`, `manufacturer`, `battery_min` y las claves de `actions`; el resto
> del bloque de cada robot lo valida su driver (`fm/adapters/<driver>/config.py`)
> fusionado con `drivers.<driver>`. Claves del formato anterior
> (`mission_group`, `positions_allowlist`, `auto_charge.mission`,
> `mqtt.manufacturer`) → error al arrancar con la pista de dónde van ahora.

```yaml
mqtt:
  fleet_manufacturer: imperial_fleet   # segmento <manufacturer> de fleet/*

auto_charge:                       # genérico: umbrales
  battery_floor: 20                # % — por debajo se encola la carga
  priority: 10
  abort_cooldown_s: 60

drivers:                           # defaults por marca
  mir:
    mission_group: "mirur-tknika"
    charge_mission: "Carga en estación"   # nombre EXACTO de la mission de carga (sin parámetros)
    # positions_allowlist: [H2D1-VL]     # opcional; si se omite, cualquier position del robot

robots:
  mir-1:
    driver: mir                    # por defecto "mir"
    host: 192.168.15.5             # o MIR_HOST_MIR_1 en .env
    battery_min: 25                # % mínimo para aceptar orders (> battery_floor)
    actions:
      ir_a:
        mission: "Ir a posición"               # nombre en la web del MiR
        position_inputs: [target_pos]          # inputs cuyo valor es un NOMBRE de position → GUID
      transporte:
        mission: "Transporte A→B"
        position_inputs: [origen, destino]
        required_inputs: [tipo_carga]          # obligatorio; ausente → validationError
  mir-2:
    host: 192.168.15.15
    battery_min: 25
    actions:
      ir_a:
        mission: "Ir a posición"
        position_inputs: [target_pos]
  sim-1:                           # opcional: robot simulado (fm/adapters/sim)
    driver: sim
    battery: 90
    actions: { ir_a: {} }
```

Semántica de cada `action` (driver `mir`):

- `mission`: nombre lógico. Al arranque se resuelve a GUID **por robot**
  (`GET /missions`, buscar por `name`). Si no existe en un robot: warning al
  arranque y `noRouteError` en runtime — el FM **arranca igual**.
- `position_inputs`: `actionParameters` cuya `value` es un nombre de position;
  se resuelve a GUID por robot (`GET /positions`). Falta → `validationError`;
  no existe / fuera de allowlist → `noRouteError`.
- `required_inputs`: se reenvían tal cual pero su ausencia es `validationError`
  (`0`/falsy es válido; solo cuenta ausente que la `key` no venga).
- El resto de `actionParameters` se pasan tal cual **si la mission expone ese
  `input_name`**; si no, se ignoran en silencio (compatibilidad hacia delante).

---

## 9. Cliente REST MiR250 — endpoints y gotchas (¡leer!)

Endpoints necesarios:

| Método | Path | Uso |
|---|---|---|
| GET | `status` | telemetría (`state_id`, `battery_percentage`, `position`, `map_id`, `mission_text`, `errors`) |
| PUT | `status` | `{"state_id": 3\|4}` reanudar / pausar |
| GET | `missions` | `[{guid, name, url}]` → índice nombre → GUID |
| GET | `missions/<guid>/actions` | parámetros de la mission: `parameters[].input_name` |
| GET | `positions` | `[{guid, name, type_id, …}]` → índice nombre → GUID |
| POST | `mission_queue` | `{"mission_id": guid, "priority": n, "parameters": [...]}` → devuelve `{id, state, …}` |
| GET | `mission_queue/<id>` | `state` ∈ `Pending \| Executing \| Done \| Aborted \| Cancelled` |
| DELETE | `mission_queue/<id>` | aborta esa entrada (queda `Aborted`) |

Gotchas verificados contra robots reales en el proyecto anterior:

1. **Parámetros de `POST /mission_queue`:** cada uno es
   `{"id": "<input_name>", "value": ...}` donde `id` es el **`input_name` de
   Blockly** (lo que ve el usuario, p.ej. `target_pos`), **NO** el `id`/schema
   interno de la action (p.ej. `position`). Con el interno el MiR devuelve
   `400 invalid_input_data / parameter_input_name_not_valid`. Usa
   `missions/<guid>/actions` solo para **verificar que el `input_name` existe**.
2. **GUIDs distintos por robot** para missions, positions, mission_groups y
   mapas aunque el nombre sea idéntico. Índices por robot, siempre.
3. **Positions con nombre duplicado** (p.ej. dos `Charging station` con
   `type_id` 20 y 21): al indexar, quedarse con la primera y avisar por log.
4. **Filtrar missions por grupo:** `GET /mission_groups/<guid>/missions`.
   `/missions?group_id=` no filtra, y `/missions` no trae `group_id`.
5. **Errores 4xx:** el MiR explica el motivo en el body JSON (`message`,
   `field_errors`). `raise_for_status()` lo tira: captura `r.json()`/`r.text`
   y mételo en el mensaje de la excepción para que llegue al log y a
   `state.errors`.
6. **`orientation` viene en grados**; VDA quiere radianes.
7. **Prioridad en `mission_queue`:** valor mayor entra más arriba en la cola.
8. **Timeouts:** `requests` con `timeout=5`; sin backoff exponencial en v1
   (basta con reintentar al tick siguiente y loguear).
9. **Token Basic:** `base64("distributor:" + sha256_hex(password))`. No
   inventarlo: pedirlo al usuario o copiarlo de la web del MiR.
10. **Missions y positions se crean en la web del MiR**, no por API. El FM solo
    las referencia por nombre.

Esqueleto mínimo del cliente (nombres a conservar):

```python
class MirClient:
    def __init__(self, host, auth, timeout=5.0):
        self.prefix = f"http://{host}/api/v2.0.0/"
        self.headers = {"Content-Type": "application/json",
                        "Accept-Language": "en_US", "Authorization": auth}
    def status_get(self) -> MirStatus: ...
    def status_put(self, state_id: int) -> dict: ...
    def missions_get(self) -> list[dict]: ...
    def missions_mission_id_actions_get(self, guid) -> list[dict]: ...
    def positions_get(self) -> list[dict]: ...
    def mission_queue_post(self, mission_guid, parameters=None, priority=0) -> dict: ...
    def mission_queue_id_get(self, queue_id) -> dict: ...
    def mission_queue_id_delete(self, queue_id) -> None: ...
    # índices cacheados al arranque
    def index_missions_by_name(self) -> dict[str, str]: ...
    def index_positions_by_name(self) -> dict[str, str]: ...
    def index_mission_params(self, mission_guid) -> set[str]: ...   # input_names
```

---

## 10. Cómo asegurar que "cumple VDA 5050"

Lo que el estándar exige y aquí es verificable:

1. **Esquema de mensajes.** Descargar los JSON Schema oficiales del repo
   `VDA5050/VDA5050` en GitHub (`json_schemas/*.schema`, rama de la v3.0.0) a
   `schemas/` y validar con `jsonschema`:
   - en tests: todo `state`/`connection`/`order_response` que genera el FM;
   - en runtime (flag `--validate`): cada `order`/`instantActions` que entra
     (rechazo con `validationError` si no valida).
   Si el PDF de la norma está disponible (`VDA5050-V3.0.0-2025-03.pdf`), los
   capítulos relevantes son: §6.3 order, §6.4 instantActions, §6.5 connection
   (Last Will), §6.6 factsheet, §7.8 state, §6.1.4 gestión de `orderUpdateId`.
2. **Topics** con la estructura `interfaceName/majorVersion/manufacturer/serialNumber/topic`,
   y **header ≡ topic** en todo mensaje, entrante y saliente (§5.1). Test
   explícito: publicar en `fleet/order` con `manufacturer: "Otro"` →
   `order_response REJECTED validationError`.
3. **`connection`** retained + Last Will `CONNECTION_BROKEN` configurado antes de conectar.
4. **`headerId`** monótono por topic; `timestamp` con formato exacto.
5. **Ciclo de vida de `orderUpdateId`** (§5.4): idempotencia, regresión, actualización.
6. **`state` con todos los campos requeridos** (aunque sean listas vacías) y
   nombres v3 (`mobileRobotPosition`, `powerSupply`).
7. **`instantActions` estándar** `startPause`, `stopPause`, `cancelOrder`,
   `stateRequest` con reflejo en `instantActionStates`.
8. **`errors[]`** con `errorType`, `errorLevel`, `errorDescription` y
   `errorReferences` apuntando al `orderId`. En v3 `errorLevel` ∈
   `WARNING | URGENT | CRITICAL | FATAL` (§7.8, p. 89 del PDF): `WARNING` = el
   robot puede seguir; `FATAL` = requiere intervención. Usa `WARNING` para
   rechazos de order y `CRITICAL`/`FATAL` para `state_id = 12` del MiR.
9. **`factsheet`** (opcional pero recomendable para "cumplimiento pleno"):
   publicar retained al arranque con `typeSpecification`, `physicalParameters`,
   `protocolFeatures.agvActions` listando los `actionType` de `fleet.yaml`.

Lo que **no** se cumple y hay que documentar en el README como limitación
conocida: navegación por grafo `nodes/edges` (el MiR navega por missions), y el
topic `fleet/order` es una extensión propia fuera del estándar.

---

## 11. Plan por hitos

Cada hito termina con: tests offline en verde, prueba manual documentada en
`docs/avance.md`, y (si toca hardware) confirmación del usuario.

- **H0 — Esqueleto.** Estructura de carpetas, `config.py` (dataclasses +
  carga YAML/.env), `mir_client.py`, `scripts/read_status.py` y
  `scripts/list_missions.py` (imprime missions con sus `input_name`s y
  positions por robot). Verificar contra los robots reales. Confirmar con el
  usuario los nombres exactos de las missions y positions y rellenar `fleet.yaml`.
- **H1 — Telemetría VDA.** `vda5050/header.py`, `adapters/mir.py::to_vda_state`,
  `vda5050/state.py`, `mqtt_publisher.py` con LWT. `run_fm.py` publicando
  `state` a 1 Hz y `connection` retained. Verificar con `mosquitto_sub`.
  Validar `state` contra el schema oficial.
- **H2 — Order dirigida.** `vda5050/order.py`, `adapters/mir.py::from_vda_order`,
  `orders.py` (estado por robot + seguimiento de `mission_queue`), `mqtt_orders.py`.
  Reglas §5.4 (incluida header ≡ topic) con tests. `scripts/send_order.py`.
  Prueba real con una mission inocua (pedir permiso).
- **H3 — Asignador de flota.** `assigner.py` (puro, con tests de todos los
  filtros y del ranking), `mqtt_fleet.py` con `order_response`.
  `scripts/send_fleet_order.py`.
- **H4 — Auto-carga.** `charge.py` + integración en el tick + `information`
  AUTO_CHARGE + resolver `powerSupply.charging`. Test offline con SoC
  simulado (35 → 18 → 17 con carga viva → Done → 80). Prueba real subiendo
  temporalmente `battery_floor` por encima del SoC actual (y revertirlo).
  Resolver el deadlock de §7 con el usuario.
- **H5 — instantActions.** `startPause`, `stopPause`, `cancelOrder`,
  `stateRequest` + `instantActionStates`. `scripts/send_instant_action.py`.
- **H6 — Cumplimiento y entrega.** Validación runtime con schemas (`--validate`),
  `factsheet` opcional, README como contrato de integración (topics, payloads,
  reglas, errores, config, smoke de integración), cierre limpio con SIGTERM.

---

## 12. Scripts de apoyo (`scripts/`)

Todos leen `.env` y `config/fleet.yaml`; todos aceptan `<serial>` como argumento.
**Los scripts construyen el header con la misma función que el servicio**
(`vda5050/header.py`), para que no puedan divergir del topic.

- `read_status.py <serial>` — `GET /status` crudo + resumen.
- `list_missions.py <serial>` — missions (nombre, GUID, `input_name`s) y positions.
- `send_order.py <serial> <actionType> key=value…` — publica `<serial>/order`.
- `send_fleet_order.py <actionType> key=value…` — publica `fleet/order` y espera `order_response`.
- `send_instant_action.py <serial> <actionType>` — publica `instantActions`.
- `read_vda_state.py [serial]` — `mosquitto_sub` en Python con resumen por línea.
- `test_assigner.py` — escenarios offline del asignador (también como pytest).

Formato de log (stdout), un prefijo por robot para poder filtrar:

```
2026-09-16 10:11:03,446 [mir-2] state hdr=742 pos=(29.80,7.96) bat=54.9% order=fleet-250ef207/0 errs=0
2026-09-16 10:11:05,120 [fleet] fleet/order fleet-abc → mir-2 (asignado a mir-2)
2026-09-16 10:12:40,003 [mir-1] batería 19.4% < 20% — 'Carga en estación' encolada (queue id=1283, priority=10)
```

---

## 13. Smoke de integración (para el README final)

1. `mosquitto_sub -t 'vda5050/v3/MiR/+/state' -v` → un mensaje/robot/segundo,
   `headerId` creciente, posición que cambia si se mueve el robot a mano.
2. Cliente limpio suscrito a `+/connection` recibe `ONLINE` retained al instante.
3. `fleet/order` válida → `order_response ASSIGNED` + mission en la cola del MiR elegido.
4. `fleet/order` con los dos robots ocupados → `REJECTED orderError` con detalle.
5. `fleet/order` con `manufacturer` o `serialNumber` que no cuadran con el
   topic → `REJECTED validationError`.
6. Segunda `fleet/order` idéntica (`orderId`,`orderUpdateId`) → ignorada.
7. `instantActions cancelOrder` con mission `Executing` → desaparece de la cola,
   `state.errors[]` recibe `orderError` con `errorReferences.orderId`.
8. Bajar `battery_floor` por encima del SoC → una única mission de carga encolada
   con prioridad alta; no se re-postea mientras vive; `information` AUTO_CHARGE.
9. Matar el FM con `kill -9` → el broker publica `CONNECTION_BROKEN` retained.

---

## 14. Referencia: qué se hereda del proyecto anterior

Existe un FM previo (`excelencia-p6/mir250/fleet_manager/`, ~2.800 líneas)
del que este brief es destilado. Si ese código está accesible en el nuevo
entorno, **puede copiarse** `mir_adapter/client.py`, `charge.py`,
`mqtt_bridge.py`, `vda5050/*.py` casi tal cual; `assigner.py` y
`adapters/mir.py` hay que **simplificarlos** quitando herramientas
(`ToolRegistry`, `swap_missions`, `factory/tools`), catálogo `pieces`,
`piece_input`/`sumergir_input` y encadenado swap → objetivo. Si no está
accesible, este documento es suficiente para reimplementarlo.

Deuda conocida de ese FM que **no** hay que heredar (ver §5.1 y §5.7):
no validaba el header contra el topic; sus scripts de prueba enviaban
`manufacturer: "FleetMaster"`; `fleet/order` iba con `serialNumber: ""`; y
`order_response` REJECTED omitía `serialNumber` mientras ASSIGNED reutilizaba
ese campo para el robot elegido.
