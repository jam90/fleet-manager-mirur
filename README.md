# Fleet Manager — VDA 5050 v3.0.0

Servicio Python autónomo que gestiona una flota de robots móviles hablando
**MQTT (VDA 5050 v3.0.0)** con el resto del sistema y, con cada robot, el
protocolo de su marca a través de un **driver enchufable** (`fm/adapters/`):
hoy MiR250 por REST y un robot simulado sin hardware; añadir otra marca es una
carpeta más. Sin ROS 2, sin MiR Fleet, sin Open-RMF. Pensado también como
material didáctico: el código está comentado explicando el *por qué*.

Todo lo de abajo está probado con dos MiR250 reales.
Detalle y decisiones en [`docs/avance.md`](docs/avance.md); brief de arranque
en [`CLAUDE.md`](CLAUDE.md).

## Qué hace

1. Publica `state` (1 Hz) y `connection` (retained + Last Will) por robot,
   cada uno bajo el `manufacturer` de su marca.
2. Acepta `order` dirigida a un robot (`<serial>/order`) o a la flota
   (`fleet/order`, extensión propia). En el segundo caso elige robot: no ocupado
   y con batería ≥ `battery_min`; entre varios, el de más batería.
3. Traduce cada `actionType` (con sus `actionParameters`) a un trabajo del
   robot a través de su driver — en MiR, una **mission ya creada en la web**
   con sus inputs — y sigue su progreso en `state.actionStates[]`.
4. Manda a cargar cuando la batería baja de `battery_floor` (sin abortar lo
   que esté en curso) y lo refleja en `powerSupply.charging`.
5. Atiende `instantActions`: `startPause`, `stopPause`, `cancelOrder`,
   `stateRequest`.
6. Tolera robots apagados (`ROBOT_UNREACHABLE`, backoff, sin perder cadencia)
   y missions lanzadas desde fuera del FM (el robot cuenta como ocupado).
7. Interfaz web en `http://<pc>:8050/`: batería y misión de cada robot en
   vivo, pausa / reanudar / cancelar, y envío de `fleet/order` con parámetros.
   Arriba, la sección **Robot** (`#robot/<serial>`): todo lo que se sabe de un
   robot (estado, velocidad, aviso si una mission espera al operario,
   identidad, odómetro, errores, la misión en curso paso a paso, la cola, las
   últimas misiones y el `state` VDA tal cual).

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
mqtt: { fleet_manufacturer: imperial_fleet }   # segmento <manufacturer> de fleet/*
auto_charge: { battery_floor: 20 }  # genérico: umbrales; el job de carga lo pone el driver
site: imperialfleet-session000-01-0000-001   # opcional: solo trabajan los robots de este site
drivers:                            # defaults por marca (cualquier robot los puede pisar)
  mir:
    mission_group: "mirur-tknika"   # solo se indexan las missions de este grupo
    charge_mission: imperialfleet-mission000-01-0001-002   # Carga en estación MIRUR
actions:                            # catálogo: cada actionType se define UNA vez
  coger: { mission: imperialfleet-mission000-01-0001-001 }   # GUID del catálogo
  dejar: { mission: "dejar" }                                # o nombre exacto
  abrir_puerta:
    mission: imperialfleet-mission000-01-0001-006   # Simular Apertura Puerta H2DX MIRUR
    position_inputs: [target_pos]   # la order trae {"key": "target_pos", "value": "H2D2"}
    expected_s: 90                  # opcional: más de 90 s en ejecución → aviso
robots:
  mir-1:
    driver: mir                     # por defecto "mir"
    host: 192.168.15.5              # ↓ a partir de aquí lo interpreta el driver
    battery_min: 25                 # > battery_floor (histéresis)
    actions: [coger, dejar, abrir_puerta]   # habilitadas en este robot
  mir-2:
    host: 192.168.15.15
    battery_min: 25
    actions: [coger, dejar]
```

El core solo lee `driver`, `manufacturer` (opcional; por defecto el del
driver), `battery_min` y la lista `actions` de cada robot (qué actionTypes
puede hacer), que tienen que estar en el catálogo `actions:`, y de cada
action del catálogo, `expected_s` (ver más abajo). Cada robot
recibe la definición del catálogo de las actions que tiene habilitadas; esa
definición, y el resto del bloque del robot, van crudos al driver, que los
valida al arrancar (`fm/adapters/mir/config.py`). No hay forma de cambiar una
action para un solo robot: si hace falta otra versión, es otra action del
catálogo. Formatos anteriores fallan al arrancar con la pista de cómo
migrar: `mission_group`, `positions_allowlist` o `auto_charge.mission` en la
raíz, y definiciones de actions dentro de cada robot (formato anterior al
2026-10-14).

Por `action` (driver `mir`):

| Clave | Significado |
|---|---|
| `mission` | GUID del catálogo (`catalog/missions/`, el mismo en toda la flota sincronizada) o nombre exacto de la mission en la web del MiR, que se resuelve a GUID **por robot** al arrancar |
| `position_inputs` | `actionParameters` cuyo valor es un **nombre de position**; se traduce a GUID. Ausente → `VALIDATION_FAILURE`; inexistente → `NO_ROUTE_TO_TARGET` |
| `required_inputs` | parámetros obligatorios que se reenvían tal cual (ausente → `VALIDATION_FAILURE`) |
| `number_inputs` | parámetros obligatorios **numéricos**: `5`, `5.0`, `"5"` o `"2,5"` se mandan al MiR como número (`5`, `2.5`); ausente o no numérico → `VALIDATION_FAILURE`. P.ej. `n_piezas_pedidas` (registro PLC 22). Lista de nombres, o mapping con rango `{n_piezas_pedidas: {min: 1, max: 10, step: 1}}`: fuera de rango → `VALIDATION_FAILURE`, y la UI pinta un deslizador |
| `positions_allowlist` | (por action) positions admitidas en los `position_inputs` de **esa** action; se suma a la del robot. Fuera de ella → `NO_ROUTE_TO_TARGET`; la UI solo ofrece estas |

**`expected_s`** (cualquier driver, opcional): segundos normales en
ejecución de la action. Si una order la supera, su robot lleva en
`state.errors[]` un `ORDER_DURATION_EXCEEDED` con `errorLevel: WARNING` y
`errorReferences` a la order y la action; el robot sigue con ella y el aviso
se va cuando termina. Solo cuenta el tiempo `RUNNING` sin pausa: no la
espera en la cola del robot (p. ej. detrás de una carga). Para elegir el
valor, `scripts/mission_durations.py <serial>` lo propone a partir de la
historia del robot. El log dice cuánto estuvo en ejecución cada order al
terminar.

El resto de `actionParameters` se reenvían solo si la mission expone ese
`input_name`; si no, se ignoran. En el `POST /mission_queue` cada parámetro va
como `{"id": <input_name>, "value": …}`: el `input_name` es el nombre que se le
da en Blockly (igual en todos los robots); el `guid` interno del parámetro
cambia de un robot a otro y el FM no lo usa. Al arrancar, el driver avisa en
el log si un input de `fleet.yaml` no existe en la mission de ese robot.

Ejemplo de order con parámetro numérico:

```json
{ "actionType": "prueba_parametros", "actionId": "a1", "blockingType": "HARD",
  "actionParameters": [ { "key": "n_piezas_pedidas", "value": 5 } ] }
``` Los GUIDs nunca van en la config: cambian
de un robot a otro aunque el nombre coincida.

Variables de entorno (`.env`): `MIR_AUTH` (o `AUTH_HEADER`), `MIR_AUTH_<SERIAL>`
y `MIR_HOST_<SERIAL>` como override por robot, `MQTT_HOST`, `MQTT_PORT`.

## Ejecución

```bash
.venv/bin/python run_fm.py                      # todos los robots, 1 Hz
.venv/bin/python run_fm.py --robot mir-1 --period 0.5
.venv/bin/python run_fm.py --config config/fleet-sim.yaml   # flota simulada, sin robots
.venv/bin/python run_fm.py --validate           # orders entrantes contra el JSON Schema oficial
```

**`--validate`.** Sin la opción, el FM comprueba de cada `order`,
`fleet/order` e `instantActions` lo que necesita para ejecutarla (header ≡
topic, ids, nodos, una action…). Con ella, además, valida el mensaje entero
contra `schemas/` (VDA 5050 v3.0.0) y rechaza con `VALIDATION_FAILURE` lo que
no cumpla, p. ej. una action sin `blockingType`. El motivo lleva los tres
primeros fallos con su ruta (`nodes/0/actions/0: 'blockingType' is a
required property`). Útil al integrar un emisor nuevo (el sistema aguas arriba): lo que
pase con `--validate` cumple la norma, no solo lo que este FM tolera. La
web y los `scripts/` ya mandan mensajes válidos.

Arranca aunque un robot no responda: publica `state` con
`ROBOT_UNREACHABLE` y lo reintenta con backoff (1, 2, 4, 8, 8… s). La parte
de red de cada robot (`Robot.poll`) corre en un hilo propio y el tick espera
como mucho 0,5 s a los robots que respondían: un robot apagado no altera la
cadencia de 1 Hz de los demás. Ctrl-C / SIGTERM →
`connection: OFFLINE`; muerte brusca → el broker publica `CONNECTION_BROKEN`.

**Un solo FM por broker.** Al arrancar, antes de conectar, el FM lee los
`connection` retenidos de `fleet` y de sus robots: si alguno está `ONLINE`,
ya hay otro FM vivo y este no arranca (código 3, con el topic y la hora en el
log). Dos FM a la vez se echarían del broker (mismo client id) y atenderían
los dos cada `fleet/order`. Si ese `ONLINE` es antiguo (p. ej. tras caerse
el broker), `--force` arranca igual.

Log (una línea por robot y tick, filtrable por `[serial]`):

```
13:25:52,791 [mir-1] order fleet-3928d0aa/0 aceptada → mission 'coger' job=1311
13:25:52,792 [fleet] fleet/order fleet-3928d0aa → mir-1
13:26:24,666 [mir-1] state hdr=37 pos=(29.82,4.52) bat=74.9% mode=AUTOMATIC drv=1 order=fleet-3928d0aa/0 acts=coger:RUNNING errs=0
```

## Contrato MQTT

Topics `vda5050/v3/<manufacturer>/<serialNumber>/<subtopic>`. `serialNumber` es
el nombre lógico de `fleet.yaml`. `<manufacturer>` es **el del robot** (norma
§6.2): lo pone su driver (`MiR` para los MiR250) o `robots.<serial>.manufacturer`.
`fleet` es un pseudo-serial (extensión propia) y, como no es un robot, va bajo
el nombre de flota `mqtt.fleet_manufacturer` (`imperial_fleet`).

| Topic | Sentido | QoS | Retained |
|---|---|---|---|
| `vda5050/v3/MiR/<serial>/state` | FM → bus, 1 Hz + al aceptar/rechazar order | 0 | no |
| `vda5050/v3/MiR/<serial>/connection` | FM → bus | 1 | sí + Last Will |
| `vda5050/v3/MiR/<serial>/order` | bus → FM | 1 | no |
| `vda5050/v3/MiR/<serial>/instantActions` | bus → FM | 1 | no |
| `vda5050/v3/MiR/<serial>/factsheet` | FM → bus, al arrancar y en `factsheetRequest` | 0 | sí |
| `vda5050/v3/imperial_fleet/fleet/order` | bus → FM | 1 | no |
| `vda5050/v3/imperial_fleet/fleet/order_response` | FM → bus | 1 | no |

El FM se suscribe a `vda5050/v3/+/+/order` e `instantActions` e ignora (log
DEBUG) cualquier `(manufacturer, serialNumber)` que no sea un robot
configurado o `(imperial_fleet, fleet)`.

**Header ≡ topic, siempre.** `manufacturer` y `serialNumber` del payload
deben coincidir con los segmentos del topic; para `fleet/*` el header es
`manufacturer: "imperial_fleet", serialNumber: "fleet"`. Si no cuadra → rechazo
`VALIDATION_FAILURE`.

### `order` aceptada (subset)

Un nodo lógico con **una** action. Se admite un nodo inicial sin actions;
nodos no `released` (horizon) → `VALIDATION_FAILURE`.

```json
{ "headerId": 1, "timestamp": "2026-09-16T11:25:52.000Z", "version": "3.0.0",
  "manufacturer": "imperial_fleet", "serialNumber": "fleet",
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
| robot en Manual / Error / EmergencyStop, o llave no en `auto` | `MOBILE_ROBOT_NOT_AVAILABLE` |
| robot ejecutando una mission lanzada desde la web | `MOBILE_ROBOT_NOT_AVAILABLE` |
| mapa activo del robot de otro site que el de la flota (`site:`) | `MOBILE_ROBOT_NOT_AVAILABLE` |
| la mission va a positions fijas de un mapa que no es el activo del robot | `NO_ROUTE_TO_TARGET` (la flota prueba otro robot) |
| más de una action pendiente | `VALIDATION_FAILURE` |
| `actionType` no mapeado en ese robot | `INVALID_ORDER_ACTION` |
| position inexistente / fuera de allowlist / mission ausente en el robot | `NO_ROUTE_TO_TARGET` |
| el robot no responde al encolar la mission (red) | `ROBOT_UNREACHABLE` |
| el robot rechaza la mission al encolarla (4xx del MiR) | `ORDER_EXECUTION_FAILED` |

Los rechazos de `<serial>/order` van a `state.errors[]` (`errorLevel:
WARNING`, `errorReferences: [{orderId}]`) hasta que se acepte otra order.
Un rechazo repetido (misma order, mismo motivo) aparece una sola vez, y se
conservan como mucho los 10 últimos distintos.

### `fleet/order_response`

```json
{ "headerId": 3, "timestamp": "…", "version": "3.0.0", "manufacturer": "imperial_fleet", "serialNumber": "fleet",
  "orderId": "fleet-3928d0aa", "orderUpdateId": 0,
  "status": "ASSIGNED", "assignedSerial": "mir-1", "description": "asignado a mir-1" }
```

```json
{ "headerId": 4, "timestamp": "…", "version": "3.0.0", "manufacturer": "imperial_fleet", "serialNumber": "fleet",
  "orderId": "fleet-43912315", "orderUpdateId": 0,
  "status": "REJECTED", "errorType": "NO_MOBILE_ROBOT_AVAILABLE",
  "errorDescription": "ningún robot puede atender la order: mir-1: ocupado; mir-2: ocupado" }
```

`order_response` es el ACK inmediato del asignador. Los fallos posteriores de
ejecución (mission `Aborted`) van a `state.errors[]` del robot como
`ORDER_EXECUTION_FAILED`. Una `fleet/order` repetida se ignora sin respuesta.

Si el robot elegido no puede traducir la order a una mission suya (le falta
la mission o la position, o no tiene cargados los índices), se prueba el
siguiente del ranking. Si fallan todos, el `REJECTED` lleva el motivo de
cada robot y el `errorType` común (p.ej. `NO_ROUTE_TO_TARGET`), o
`NO_MOBILE_ROBOT_AVAILABLE` si los motivos son distintos. En dos casos no se
prueba otro robot: un `VALIDATION_FAILURE` (la order está mal y ningún robot
la aceptaría) y un fallo al encolar la mission en el robot (tras un timeout el
MiR pudo encolarla igual, y acabarían dos robots con la misma order).

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
| `mode_key_state` (llave) `auto` / `manual` / `idle` (neutra) | `operatingMode: AUTOMATIC` / `MANUAL` / `INTERVENED` |
| `map_id` | `mobileRobotPosition.mapId` |
| `mission_text` | `information[] {infoType: MISSION}` |
| `errors[]` | `errors[] {errorType: MIR_<code>}` |
| `mission_queue/<id>.state` de la order | `actionStates[].actionStatus`: Pending→WAITING, Executing→RUNNING, Done→FINISHED, Aborted/Cancelled→FAILED |
| `mission_queue/<id>.message` al terminar | `actionStates[].actionResult` y final de `errorDescription` (p.ej. `Aborted - User Request` = parada a mano). El MiR usa `Aborted` para todo; el motivo solo está aquí |

`powerSupply.charging` solo es `true` cuando la mission de carga del FM está
en ejecución (pendiente de validar en el dock).

### Tipos de error que emite el FM

Predefinidos v3: `VALIDATION_FAILURE`, `INVALID_ORDER_ACTION`,
`OUTDATED_ORDER_UPDATE`, `OTHER_ORDER_ACTIVE`, `NO_ROUTE_TO_TARGET`,
`MOBILE_ROBOT_NOT_AVAILABLE`. Propios (mismo estilo):
`NO_MOBILE_ROBOT_AVAILABLE` (asignador sin candidatos),
`ORDER_EXECUTION_FAILED` (mission Aborted/Cancelled), `MIR_<code>` (errores
del MiR), `ROBOT_UNREACHABLE`, `ORDER_DURATION_EXCEEDED` (WARNING: la order
supera el `expected_s` de su action), `ROBOT_OUTSIDE_SITE` (WARNING: el mapa
activo del robot es de otro site que el de la flota; no recibe orders).

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
| `send_instant_action.py <serial> <actionType>…` | publica `instantActions` y muestra `instantActionStates` (**`stopPause` mueve el robot**) |
| `read_vda_state.py [serial]` | resume `state`/`connection` por línea y valida contra el schema |
| `mission_durations.py <serial> [--last N]` | solo lectura: duración real de cada action en la historia del robot y `expected_s` propuesto |

## Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
```

Sin red: cliente REST, header/topic, `state` contra el schema oficial,
traducción order → mission, ciclo `orderUpdateId`, asignador, configuración,
Dispatcher con bus falso. `tests/adapters/test_contract.py` ejecuta el mismo
escenario (connect → poll → translate → execute → job_status → cancel) contra
cada driver (`sim` y `mir` con cliente falso): es lo que debe pasar un driver
nuevo. (La variable evita que pytest cargue plugins de ROS presentes en el
entorno.)

## Smoke de integración

1. `mosquitto_sub -t 'vda5050/v3/MiR/+/state' -v` → un mensaje/robot/s, `headerId` creciente.
2. Cliente nuevo en `+/connection` recibe `ONLINE` retained al instante.
3. `send_fleet_order.py coger` → `ASSIGNED` + mission en la cola del MiR elegido. ✔
4. Con ambos ocupados → `REJECTED NO_MOBILE_ROBOT_AVAILABLE` con detalle. ✔
5. Repetir la misma order → ignorada. ✔
6. Header con `manufacturer`/`serialNumber` ≠ topic → `REJECTED VALIDATION_FAILURE`.
7. `kill -9` al FM → `CONNECTION_BROKEN` retained en cada `connection`. ✔

## Añadir una marca (driver)

El core no sabe de MiR: habla con cada robot a través de `fm/adapters/base.py`
(`RobotDriver`, un `Protocol`; tipos `Telemetry`, `Job`, `JobStatus`).
Añadir una marca es crear una carpeta y registrarla:

1. **`fm/adapters/<marca>/`** con una clase que cumpla `RobotDriver`
   (no hace falta heredar):

   | Método | Qué hace | Regla |
   |---|---|---|
   | `manufacturer` | segmento `<manufacturer>` de sus topics | atributo; `robots.<s>.manufacturer` lo pisa |
   | `connect() -> bool` | índices, handshake… | tolerante; mientras sea False el core lo reintenta cada 5 s (si el robot responde) |
   | `poll() -> Telemetry \| None` | snapshot normalizado | None = inalcanzable (motivo en `last_error`); nunca lanza |
   | `translate(Action) -> Job` | valida y prepara sin red | lanza `OrderRejected(errorType, texto)` |
   | `execute(Job, priority) -> str` | lanza el job | devuelve un `job_id` opaco; si el robot no lo acepta, lanza (el core lo convierte en rechazo de la order) |
   | `job_status(job_id) -> JobStatus \| None` | `WAITING/RUNNING/FINISHED/FAILED` | None = no se pudo consultar (se reintenta) |
   | `cancel(job_id)` | aborta el job (H5) | |
   | `charge_job() -> Job \| None` | job de auto-carga (H4) | None = no soporta |
   | `extra_state(State)` | gancho para campos que el core no deduce | puede no hacer nada |

   `Telemetry.available` (puede aceptar orders) y `foreign_busy` (ejecuta
   algo que no lanzó el FM) los decide el driver: son las dos cosas que el
   asignador necesita y que cada marca expresa a su manera.

2. **`make_driver(serial, raw, defaults, env)`** en el `__init__.py` del
   paquete: `raw` es el bloque `robots.<serial>` completo, `defaults` es
   `drivers.<marca>`. Valida ahí y lanza `ConfigError` con un mensaje claro.
   Convención: `raw["actions"]` es un mapping `actionType → config de la
   marca`. El core lo construye con las definiciones del catálogo `actions:`
   de las actions habilitadas en el robot; el driver solo las valida.

3. **Registrar** en `fm/adapters/__init__.py`: `DRIVERS["<marca>"] = "fm.adapters.<marca>"`.

4. **Añadir el driver a `tests/adapters/test_contract.py`** (una fixture que
   lo construya con su cliente falso). Si pasa el contrato, el core funciona
   con él. `fm/adapters/sim/` es el ejemplo mínimo (sin red, ~150 líneas);
   `fm/adapters/mir/` el completo (REST, índices por robot, cola de missions).

El `state` VDA lo construye siempre el core (`fm/vda5050/state_builder.py`)
a partir de `Telemetry` + lo que sabe de la order: un driver no puede
publicar un `state` mal formado.

### `instantActions` (bus → FM)

Mismo header que la order, campo `actions[]` con la misma forma que las
actions de una order. Soportadas:

| `actionType` | Efecto | Resultado |
|---|---|---|
| `startPause` | el robot se detiene sin abortar (MiR: `PUT /status {state_id: 4}`) | `FINISHED` |
| `stopPause` | reanuda (`state_id: 3`) | `FINISHED` |
| `cancelOrder` | aborta el job de la order activa (MiR: `DELETE /mission_queue/<id>`); la action pasa a `FAILED` sin error de ejecución | `FINISHED`, o `FAILED` + `NO_ORDER_TO_CANCEL` si no hay order |
| `stateRequest` | publica un `state` inmediatamente | `FINISHED` |
| `factsheetRequest` | vuelve a publicar el `factsheet` retained | `FINISHED` |
| otros | no soportado | `FAILED` + `INVALID_INSTANT_ACTION` |

El resultado va en `state.instantActionStates[]` y los fallos en
`state.errors[]` con `errorReferences: [{actionId}]`; se conservan hasta el
siguiente mensaje `instantActions`. Un mensaje malformado deja un
`VALIDATION_FAILURE` suelto en `errors[]`.

## Interfaz web

`run_fm.py` sirve una página en `http://<pc>:8050/` (`--web-port`, 0 = sin
web) para la LAN, sin login. Un fichero (`fm/web/static/index.html`, Vue 3
incluida en el repo: funciona sin internet) que muestra por robot batería,
estado, order y `actionStates`, texto de la misión y errores, con botones
⏸ ▶ ✖, un panel "Enviar a la flota" (action + parámetros, con desplegable de
positions cuando el driver las conoce) y una lista de eventos.

La UI es **un cliente VDA más**: recibe por WebSocket (`/ws`) exactamente lo
que el FM publica por MQTT (`{topic, payload}`), y lo que envía
(`POST /api/fleet/order`, `POST /api/robots/<serial>/instant`) el servidor lo
publica en el broker con header VDA; el FM lo recibe por su suscripción normal.
Nada de lo que hace la UI es un camino aparte: se ve en `mosquitto_sub`.
`GET /api/fleet` da el catálogo (robots, actions y parámetros, umbrales);
`/api/docs` es el OpenAPI. Un robot de otra marca aparece solo: la página no
sabe nada de MiR.

Tres pestañas más, para el profesor y no para el operario (en la barra lateral,
debajo; enlazables con `#crear`, `#configurador` y `#sincronizacion`):

- **Crear** (`/api/create`): da de alta en el MiR elegido una mission vacía
  (en un grupo del catálogo), una position (donde está el robot, en su mapa
  activo si es del catálogo) o una zona del mapa (prohibida, preferida, de
  velocidad…: un cuadrado de 1 m delante del robot que se termina de dibujar
  en el editor del mapa) con GUID de la flota, y enlaza a su
  página en la web del MiR para terminarla (`/missions/<guid>` o el
  editor del mapa, `/maps/<guid>`). Queda como borrador en `catalog/`, que reserva su número
  de GUID; **Capturar** lo relee del robot al terminar y lo guarda en el
  catálogo, listo para Sincronización y Configurador. **Borrar** quita lo
  creado allí de los robots y del catálogo (con confirmación; no si lo usa
  `fleet.yaml` u otro recurso, ni con un robot sin conexión u ocupado).
  **Nuevo mapa** (`POST /api/create/map`) sube un PNG en gris de 8 bits,
  con su resolución y su origen, a un site del catálogo o a uno nuevo: solo
  escribe en el catálogo, y se lleva a los robots en Sincronización.
  **Capturar de un robot** (`POST /api/create/<serial>/capture-map`) trae al
  catálogo un mapa hecho en la web de un MiR, con sus positions y zonas;
  solo lee el robot, y volver a capturarlo actualiza la copia.
  **Nuevo grupo** (`POST /api/create/group`) da de alta un grupo de
  missions en el catálogo; llega a cada robot al crear en él su primera
  mission, que va al site de la flota.
  En **Sincronización**, con el robot ocupado solo se despliega lo que hay
  que crear; corregir lo que ya tiene espera a que quede libre. El estado
  de la flota marca las missions **sobrantes** de cada robot (su
  `fleet.yaml` no las pide) con un botón para **quitarlas de ese robot**
  (siguen en el catálogo), y cada mission, position o zona del catálogo tiene
  **Retirar de la flota** (se borra de los robots y del catálogo; su número
  de GUID queda reservado en `catalog/retirados.json`).

- **Configurador** (`GET /api/config`): la matriz action × robot de
  `fleet.yaml`, con una marca por casilla que dice si el robot tiene grabada
  la mission y lo que usa (✓ / ✗ falta / ! distinta), según la última
  comprobación de Sincronización; los umbrales, los robots y el catálogo de
  actions. **Editar** permite cambiar las casillas, el `battery_min` de cada
  robot y los umbrales, y crear, editar o borrar actions del catálogo con un
  formulario que propone los inputs de la mission elegida (`POST
  /api/config`; una action no se renombra, y solo se borra si ningún robot
  la tiene): se valida con el mismo cargador
  del arranque, se deja copia `config/fleet.yaml.<fecha>.bak` y en el
  fichero solo cambia el valor editado (comentarios y formato intactos). Se
  aplica **en caliente**, entre dos ticks. En el panel Robots se dan de alta
  y de baja robots y se cambia su host: eso es estructural (también
  `drivers:` y MQTT), se guarda y la pestaña ofrece **Reiniciar el FM
  ahora** (`POST /api/restart`: cierre limpio y el proceso se relanza con
  los mismos argumentos). Si `fleet.yaml` se edita a mano con el FM en
  marcha, la pestaña lo detecta y ofrece **Aplicar el fichero**.
- **Sincronización**: lleva a cada robot lo que hay en `catalog/` (missions,
  positions, mapas…) con los mismos GUIDs en toda la flota
  (`docs/plan-admin.md` §3).

### `factsheet` (FM → bus, retained)

Ficha del robot y del protocolo (§6.11), construida por el core a partir de
dos ganchos opcionales del driver: `describe_robot()` (serie, cinemática,
carga, velocidades, dimensiones — para MiR250, los de la hoja de datos) y
`describe_actions()`. `protocolFeatures.mobileRobotActions` lista los
`actionType` de `fleet.yaml` (scope `NODE`, con sus `actionParameters`) y las
instantActions soportadas (scope `INSTANT`); `protocolLimits` refleja los
límites del FM (una action por order, sin edges). Válido contra el schema
oficial (con el parche `pauseAllowed`/`cancelAllowed` → boolean, ver
`schemas/README.md`).

## Auto-carga

En cada tick, por robot (`fm/charge.py`): si `battery < auto_charge.battery_floor`
y el robot está disponible, el FM pide al driver su job de carga
(`driver.charge_job()`; en MiR es `drivers.mir.charge_mission`) y lo encola
con `auto_charge.priority`. Reglas:

- **No aborta nada**: si hay una order en curso, la carga entra detrás y
  arranca al terminar. `battery_min` del asignador evita que le lleguen
  orders nuevas mientras tanto.
- La carga del FM cuenta como **ocupado** para el asignador mientras viva.
  Mientras tanto `state.information[]` lleva `AUTO_CHARGE` y, cuando el job
  está en ejecución, `powerSupply.charging = true`.
- Si la carga termina `FAILED` (abortada desde la web, dock ocupado…) o no
  se puede postear, se espera `auto_charge.abort_cooldown_s` antes de reintentar.
- Un driver que devuelva `None` en `charge_job()` desactiva la auto-carga
  para ese robot (log al primer intento).

La mission de carga del MiR debe terminar sola (porcentaje objetivo o tiempo
mínimo, **no** "cargar hasta nueva mission"): si no, nunca pasa a `Done`, el
robot queda ocupado para siempre y no sale del dock. La de `mirur-tknika`
(`Carga en estación MIRUR`) carga hasta el 70 % y sale del dock.

## Limitaciones conocidas

- Sin navegación por grafo `nodes`/`edges`: el MiR navega con sus missions; la
  order es un nodo lógico con una action.
- `fleet/order` y `order_response` son extensión propia fuera del estándar.
- Sin cola interna: si ningún robot puede, se rechaza y el sistema aguas
  arriba reencola. Sin coordinación de tráfico entre robots.
- `safetyState.fieldViolation` siempre `false` (el MiR no lo expone).
- `operatingMode`: con la llave en neutra se publica `INTERVENED`, pero el FM
  **rechaza orders** (la norma las permite en ese modo). Y en `MANUAL` la
  norma pide borrar la order; el MiR la conserva en su cola.
- El `headerId` del Last Will se fija al conectar, así que llega menor que el
  último `ONLINE`.

## Layout

```
run_fm.py               punto de entrada
fm/config.py            .env + fleet.yaml → dataclasses
fm/config_edit.py       edición de fleet.yaml desde el configurador (solo el valor editado)
fm/reload.py            aplicar una config nueva en caliente, entre ticks
fm/robot.py             runtime por robot: driver + OrderTracker + última Telemetry
fm/orders.py            OrderTracker: ciclo orderUpdateId + seguimiento del job (vía driver)
fm/assigner.py          assign(): función pura
fm/fleet.py             Dispatcher: <serial>/order, fleet/order → order_response
fm/mqtt_bus.py          paho: publish, LWT por robot, inbox (cola) de entrantes
fm/adapters/base.py     frontera core ↔ marca: Telemetry, Job, RobotDriver (Protocol)
fm/adapters/mir/        driver MiR250: client.py (REST), translate.py (puro), driver.py, config.py
fm/adapters/sim/        driver simulado (sin hardware): config/fleet-sim.yaml
fm/web/                 interfaz web: server.py (FastAPI, espejo MQTT ↔ WebSocket), static/index.html
fm/mir_client.py        shim: re-exporta fm/adapters/mir/client.py (lo usan scripts/)
fm/vda5050/             header.py (topic≡header), state.py, state_builder.py, order.py, schemas.py
schemas/                JSON Schemas oficiales v3.0.0 (con parches, ver schemas/README.md)
scripts/, tests/, docs/avance.md
```
