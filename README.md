# Fleet Manager MiR250 — VDA 5050 v3.0.0

Servicio Python autónomo que gestiona una flota de MiR250 hablando **REST** con
los robots y **MQTT (VDA 5050 v3.0.0)** con el resto del sistema. Sin ROS 2, sin
MiR Fleet, sin Open-RMF. Pensado también como material didáctico: el código
está comentado explicando el *por qué*.

Estado actual: telemetría, order dirigida, asignador de flota — probado con
dos robots reales. Pendiente: auto-carga, instantActions, factsheet. Detalle
y decisiones en [`docs/avance.md`](docs/avance.md); brief completo en
[`CLAUDE.md`](CLAUDE.md).

## Qué hace

1. Publica `state` (1 Hz) y `connection` (retained + Last Will) por robot.
2. Acepta `order` dirigida a un robot (`<serial>/order`) o a la flota
   (`fleet/order`, extensión propia). En el segundo caso elige robot: no ocupado
   y con batería ≥ `battery_min`; entre varios, el de más batería.
3. Traduce cada `actionType` a una **mission ya creada en la web del MiR** y
   la encola por REST; sigue la cola y refleja el progreso en
   `state.actionStates[]`.
4. (Pendiente) Manda a cargar cuando la batería baja de `battery_floor`.

## Instalación

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env         # rellenar MIR_AUTH (o AUTH_HEADER) con el token Basic del MiR
```

Token: `base64("distributor:" + sha256_hex(password))`, o copiarlo de la web
del MiR (Help → API → Authorize). Broker de desarrollo: Mosquitto en
`localhost:1883` sin auth.

## Configuración (`config/fleet.yaml`)

```yaml
mission_group: "mirur-tknika"       # solo se indexan las missions de este grupo
mqtt: { manufacturer: MiR }
auto_charge:
  mission: "Carga en estación MIRUR"
  battery_floor: 20
robots:
  mir-1:
    host: 192.168.15.5
    battery_min: 25                 # > battery_floor (histéresis)
    actions:
      coger: { mission: "coger" }
      dejar: { mission: "dejar" }
```

Por `action`:

| Clave | Significado |
|---|---|
| `mission` | nombre exacto de la mission en la web del MiR; se resuelve a GUID **por robot** al arrancar |
| `position_inputs` | `actionParameters` cuyo valor es un **nombre de position**; se traduce a GUID. Ausente → `VALIDATION_FAILURE`; inexistente → `NO_ROUTE_TO_TARGET` |
| `required_inputs` | parámetros obligatorios que se reenvían tal cual (ausente → `VALIDATION_FAILURE`) |

El resto de `actionParameters` se reenvían solo si la mission expone ese
`input_name`; si no, se ignoran. Los GUIDs nunca van en la config: cambian
de un robot a otro aunque el nombre coincida.

Variables de entorno (`.env`): `MIR_AUTH` (o `AUTH_HEADER`), `MIR_AUTH_<SERIAL>`
y `MIR_HOST_<SERIAL>` como override por robot, `MQTT_HOST`, `MQTT_PORT`.

## Ejecución

```bash
.venv/bin/python run_fm.py                      # todos los robots, 1 Hz
.venv/bin/python run_fm.py --robot mir-1 --period 0.5
```

Arranca aunque un robot no responda: publica `state` con
`MIR_REST_UNREACHABLE` y reintenta los índices en cada tick. Ctrl-C / SIGTERM →
`connection: OFFLINE`; muerte brusca → el broker publica `CONNECTION_BROKEN`.

Log (una línea por robot y tick, filtrable por `[serial]`):

```
13:25:52,791 [mir-1] order fleet-3928d0aa/0 aceptada → 'coger' queue id=1311
13:25:52,792 [fleet] fleet/order fleet-3928d0aa → mir-1
13:26:24,666 [mir-1] state hdr=37 pos=(29.82,4.52) bat=74.9% state_id=5 order=fleet-3928d0aa/0 acts=coger:RUNNING errs=0
```

## Contrato MQTT

Topics `vda5050/v3/<manufacturer>/<serialNumber>/<subtopic>`. `serialNumber` es
el nombre lógico de `fleet.yaml`; `fleet` es un pseudo-serial (extensión propia).

| Topic | Sentido | QoS | Retained |
|---|---|---|---|
| `vda5050/v3/MiR/<serial>/state` | FM → bus, 1 Hz + al aceptar/rechazar order | 0 | no |
| `vda5050/v3/MiR/<serial>/connection` | FM → bus | 1 | sí + Last Will |
| `vda5050/v3/MiR/<serial>/order` | bus → FM | 1 | no |
| `vda5050/v3/MiR/<serial>/instantActions` | bus → FM (pendiente) | 1 | no |
| `vda5050/v3/MiR/fleet/order` | bus → FM | 1 | no |
| `vda5050/v3/MiR/fleet/order_response` | FM → bus | 1 | no |

**Header ≡ topic, siempre.** `manufacturer` y `serialNumber` del payload
deben coincidir con los segmentos del topic; para `fleet/*` el header es
`manufacturer: "MiR", serialNumber: "fleet"`. Si no cuadra → rechazo
`VALIDATION_FAILURE`.

### `order` aceptada (subset)

Un nodo lógico con **una** action. Se admite un nodo inicial sin actions;
nodos no `released` (horizon) → `VALIDATION_FAILURE`.

```json
{ "headerId": 1, "timestamp": "2026-09-16T11:25:52.000Z", "version": "3.0.0",
  "manufacturer": "MiR", "serialNumber": "fleet",
  "orderId": "fleet-3928d0aa", "orderUpdateId": 0,
  "nodes": [ { "nodeId": "N0", "sequenceId": 0, "released": true,
               "actions": [ { "actionType": "coger", "actionId": "act-1",
                              "blockingType": "HARD", "actionParameters": [] } ] } ],
  "edges": [] }
```

Reglas (§6.1.4 de la norma):

| Situación | Resultado |
|---|---|
| mismo `orderId` + mismo `orderUpdateId` | se ignora en silencio |
| mismo `orderId`, `orderUpdateId` menor | `OUTDATED_ORDER_UPDATE` |
| mismo `orderId`, `orderUpdateId` mayor, order viva | `VALIDATION_FAILURE` (v1 no amplía orders en curso) |
| mismo `orderId`, `orderUpdateId` mayor, order terminada | se acepta; se ejecuta la primera action no `FINISHED` |
| otro `orderId` con order viva | `OTHER_ORDER_ACTIVE` |
| robot en Manual / Error / EmergencyStop | `MOBILE_ROBOT_NOT_AVAILABLE` |
| robot ejecutando una mission lanzada desde la web | `MOBILE_ROBOT_NOT_AVAILABLE` |
| más de una action pendiente | `VALIDATION_FAILURE` |
| `actionType` no mapeado en ese robot | `INVALID_ORDER_ACTION` |
| position inexistente / fuera de allowlist / mission ausente en el robot | `NO_ROUTE_TO_TARGET` |

Los rechazos de `<serial>/order` van a `state.errors[]` (`errorLevel:
WARNING`, `errorReferences: [{orderId}]`) hasta que se acepte otra order.

### `fleet/order_response`

```json
{ "headerId": 3, "timestamp": "…", "version": "3.0.0", "manufacturer": "MiR", "serialNumber": "fleet",
  "orderId": "fleet-3928d0aa", "orderUpdateId": 0,
  "status": "ASSIGNED", "assignedSerial": "mir-1", "description": "asignado a mir-1" }
```

```json
{ "headerId": 4, "timestamp": "…", "version": "3.0.0", "manufacturer": "MiR", "serialNumber": "fleet",
  "orderId": "fleet-43912315", "orderUpdateId": 0,
  "status": "REJECTED", "errorType": "NO_MOBILE_ROBOT_AVAILABLE",
  "errorDescription": "ningún robot puede atender la order: mir-1: ocupado; mir-2: ocupado" }
```

`order_response` es el ACK inmediato del asignador. Los fallos posteriores de
ejecución (mission `Aborted`) van a `state.errors[]` del robot como
`ORDER_EXECUTION_FAILED`. Una `fleet/order` repetida se ignora sin respuesta.

**Todos los robots ocupados → `REJECTED NO_MOBILE_ROBOT_AVAILABLE` y el FM
olvida la order.** No hay cola interna ni reintentos: el sistema aguas arriba
recibe el rechazo y decide cuándo reenviar (puede vigilar `+/state` y esperar
a un robot sin `actionStates` en `WAITING`/`RUNNING`). Un reenvío con el mismo
`orderId` se acepta con normalidad si ya hay robot libre: la idempotencia solo
ignora repeticiones de la última order **aceptada**.

### `state` (resumen del mapeo)

| MiR `/status` | VDA `state` |
|---|---|
| `state_id` 5 Executing / 4 Pause / 11 Manual / 12 Error / 10 EStop | `driving` / `paused` / `operatingMode: MANUAL` / `errors[] FATAL` / `safetyState.activeEmergencyStop: MANUAL` |
| `position.{x,y,orientation°}` | `mobileRobotPosition.{x,y,theta rad}` |
| `battery_percentage` | `powerSupply.stateOfCharge` |
| `map_id` | `mobileRobotPosition.mapId` |
| `mission_text` | `information[] {infoType: MISSION}` |
| `errors[]` | `errors[] {errorType: MIR_<code>}` |
| `mission_queue/<id>.state` de la order | `actionStates[].actionStatus`: Pending→WAITING, Executing→RUNNING, Done→FINISHED, Aborted/Cancelled→FAILED |

`powerSupply.charging` solo es `true` cuando la mission de carga del FM está
en ejecución (pendiente de validar en el dock).

### Tipos de error que emite el FM

Predefinidos v3: `VALIDATION_FAILURE`, `INVALID_ORDER_ACTION`,
`OUTDATED_ORDER_UPDATE`, `OTHER_ORDER_ACTIVE`, `NO_ROUTE_TO_TARGET`,
`MOBILE_ROBOT_NOT_AVAILABLE`. Propios (mismo estilo):
`NO_MOBILE_ROBOT_AVAILABLE` (asignador sin candidatos),
`ORDER_EXECUTION_FAILED` (mission Aborted/Cancelled), `MIR_<code>` (errores
del MiR), `MIR_REST_UNREACHABLE`.

## Scripts (`scripts/`)

Todos leen `.env` y `config/fleet.yaml`. Los que publican MQTT construyen el
header con la misma función que el servicio.

| Script | Uso |
|---|---|
| `read_status.py <serial> [--raw]` | `GET /status` + resumen |
| `list_missions.py <serial> [--all]` | missions del grupo (con `input_name`s de las de `fleet.yaml`) y positions |
| `run_mission.py <serial> <mission> [k=v…]` | encola una mission por REST y la sigue hasta terminar (**mueve el robot**) |
| `send_order.py <serial> <actionType> [k=v…]` | publica `<serial>/order` y muestra el `state` resultante |
| `send_fleet_order.py <actionType> [k=v…]` | publica `fleet/order` y espera `order_response` |
| `read_vda_state.py [serial]` | resume `state`/`connection` por línea y valida contra el schema |

## Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
```

Sin red: cliente REST, header/topic, `state` contra el schema oficial,
traducción order → mission, ciclo `orderUpdateId`, asignador. (La variable
evita que pytest cargue plugins de ROS presentes en el entorno.)

## Smoke de integración

1. `mosquitto_sub -t 'vda5050/v3/MiR/+/state' -v` → un mensaje/robot/s, `headerId` creciente.
2. Cliente nuevo en `+/connection` recibe `ONLINE` retained al instante.
3. `send_fleet_order.py coger` → `ASSIGNED` + mission en la cola del MiR elegido. ✔
4. Con ambos ocupados → `REJECTED NO_MOBILE_ROBOT_AVAILABLE` con detalle. ✔
5. Repetir la misma order → ignorada. ✔
6. Header con `manufacturer`/`serialNumber` ≠ topic → `REJECTED VALIDATION_FAILURE`.
7. `kill -9` al FM → `CONNECTION_BROKEN` retained en cada `connection`. ✔

## Limitaciones conocidas

- Sin navegación por grafo `nodes`/`edges`: el MiR navega con sus missions; la
  order es un nodo lógico con una action.
- `fleet/order` y `order_response` son extensión propia fuera del estándar.
- Sin cola interna: si ningún robot puede, se rechaza y el sistema aguas
  arriba reencola. Sin coordinación de tráfico entre robots.
- `safetyState.fieldViolation` siempre `false` (el MiR no lo expone).
- El `headerId` del Last Will se fija al conectar, así que llega menor que el
  último `ONLINE`.

## Layout

```
run_fm.py               punto de entrada
fm/config.py            .env + fleet.yaml → dataclasses
fm/mir_client.py        REST MiR250 (MirClient, MirStatus, índices nombre→GUID)
fm/robot.py             runtime por robot: cliente, índices, tracker, busy/snapshot
fm/orders.py            OrderTracker: ciclo orderUpdateId + seguimiento de mission_queue
fm/assigner.py          assign(): función pura
fm/fleet.py             Dispatcher: <serial>/order, fleet/order → order_response
fm/mqtt_bus.py          paho: publish, LWT por robot, inbox (cola) de entrantes
fm/adapters/mir.py      MirStatus → State; Action → MissionRequest (puro)
fm/vda5050/             header.py (topic≡header), state.py, order.py, schemas.py
schemas/                JSON Schemas oficiales v3.0.0 (con parches, ver schemas/README.md)
scripts/, tests/, docs/avance.md
```
