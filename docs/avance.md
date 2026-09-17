# Bitácora de avance — Fleet Manager MiR250 (VDA 5050 v3.0.0)

Formato: fecha · decisión · motivo. Se escribe según se decide, no al cerrar hitos.

## 2026-09-16 — Revisión del estándar VDA 5050 v3.0.0 antes de codificar

Fuentes: `VDA5050-V3.0.0-2025-03.pdf` (texto extraído), JSON Schemas oficiales
de la tag `3.0.0` del repo `VDA5050/VDA5050` (copiados a `schemas/`), y las
missions de ejemplo de `ejemplos_mision/`.

### Lo que confirma el brief (`CLAUDE.md`)

- Topics `interfaceName/majorVersion/manufacturer/serialNumber/topic` (§4.2).
- `connection` retained + Last Will `CONNECTION_BROKEN` fijado antes de conectar,
  `ONLINE` al conectar, `OFFLINE` al cerrar limpio (§6.5). QoS 1.
- `headerId` por topic, `timestamp` `YYYY-MM-DDTHH:mm:ss.fffZ` (§7.2).
- Nombres v3: `mobileRobotPosition`, `powerSupply` (§7.8).
- `startPause`, `stopPause`, `cancelOrder` obligatorios en todo robot (§6.2.3).
- Ciclo `orderUpdateId`: idempotencia (mismo contenido → ignorar), regresión,
  otra order activa (§6.1.4). Los rechazos van a `state.errors` con nivel `WARNING`.
- Niveles de error `WARNING | URGENT | CRITICAL | FATAL` (§6.6.5.1).

### Lo que cambia respecto al brief (decisiones tomadas hoy)

1. **Nombres de `errorType`.** v3 define tipos predefinidos en MAYÚSCULAS
   (§6.6.5.4, tabla 9); los del brief (`validationError`, `noRouteError`,
   `orderError`) eran de v2. Se usan los v3:
   `VALIDATION_FAILURE`, `INVALID_ORDER_ACTION`, `OUTDATED_ORDER_UPDATE`,
   `SAME_ORDER_UPDATE_ID`, `OTHER_ORDER_ACTIVE`, `NO_ROUTE_TO_TARGET`,
   `MOBILE_ROBOT_NOT_AVAILABLE`, `NO_ORDER_TO_CANCEL`, `INVALID_INSTANT_ACTION`,
   `ORDER_UPDATE_FOLLOWING_CANCEL`, `LOCALIZATION_ERROR`.
   `errorType` es un enum extensible, así que se añaden tres propios (mismo
   estilo): `NO_MOBILE_ROBOT_AVAILABLE` (asignador sin candidatos),
   `ORDER_EXECUTION_FAILED` (mission `Aborted`/`Cancelled` en el MiR) y
   `MIR_<code>` (errores del propio MiR, nivel `FATAL`).
2. **Persistencia de errores de rechazo.** La norma dice "se reporta hasta que
   se acepte una nueva order" (order) o "hasta que se acepte una nueva
   instantAction" (instant). Se implementa así: dos listas separadas en el
   tracker de cada robot, cada una vaciada por su evento.
3. **`factsheet` es obligatorio en v3** (§4.3, tabla 2). Pasa de "opcional" a
   parte del alcance: se publica retained al arranque y ante `factsheetRequest`.
4. **`instantActions[].blockingType` solo admite `NONE`** (schema oficial). El
   ejemplo del brief llevaba `HARD`; los scripts envían `NONE`.
5. **`safetyState` es obligatorio** en `state` (`activeEmergencyStop`,
   `fieldViolation`). Se mapea `activeEmergencyStop = MANUAL` si `state_id == 10`
   (EmergencyStop del MiR), `NONE` en otro caso; `fieldViolation = false` (el
   MiR no lo expone en `/status`; documentado como limitación).
6. **`information[].infoDescriptor`**, no `infoDescription` (schema v3).
7. **`clearInstantActions`** (§7.8): los `instantActionStates` se conservan
   hasta que llega esta instantAction. Se implementa junto a las del brief.
8. **QoS.** La norma pide QoS 0 para `order`/`instantActions`/`state`/`factsheet`
   y QoS 1 solo para `connection` (§4.1). El FM publica `state` y `factsheet`
   con QoS 0 y `connection` con QoS 1; se suscribe a `order`/`instantActions`
   con QoS 1 (el broker entrega con el mínimo entre publicador y suscriptor,
   así aceptamos ambos). `fleet/order_response` es extensión propia: QoS 1.
9. **`cancelOrder`** sigue el ciclo del §6.1.3: `RUNNING` mientras el MiR
   aborta, `FINISHED` cuando la entrada de `mission_queue` pasa a
   `Aborted`; las actions de la order pasan a `FAILED`; `orderId` y
   `orderUpdateId` se conservan. **No** se añade un `orderError` a
   `state.errors` (el brief lo pedía; la norma no, y ensuciaría `errors[]`
   con algo que no es un error). Si no hay order activa o el `orderId` del
   parámetro no coincide → `FAILED` + error `NO_ORDER_TO_CANCEL` con
   `errorReferences: [{actionId}]`.
10. **Modelo de nodos.** Se mantiene "un nodo lógico con actions" pero se
    valida lo que la norma exige y es barato: `sequenceId` del primer nodo = 0,
    primer nodo `released`, `len(edges) == len(nodes) - 1`. Se admite un nodo
    inicial sin actions (el "nodo donde está el robot", como quiere §6.1.1) y
    se ejecuta la **única** action de los nodos `released`. Horizon (nodos no
    `released`) → `VALIDATION_FAILURE` (el MiR no puede retener un horizonte).
11. **Order update (`orderUpdateId > 0`).** Solo se acepta con el robot idle;
    se ejecuta la primera action no `FINISHED` (por `actionId`) de los nodos
    `released`. Si la order sigue viva → `VALIDATION_FAILURE` explicando que
    v1 no admite ampliar una order en curso. Documentado como no probado en
    hardware.
12. **Order con el robot en `MANUAL`/`Error`/`EmergencyStop`** →
    `MOBILE_ROBOT_NOT_AVAILABLE` (§6.1.4.9).
13. **`state` por eventos.** Además del tick a 1 Hz (≪ 30 s máximos, §6.6)
    se publica `state` inmediatamente al aceptar/rechazar una order y al
    procesar instantActions, como pide la lista de eventos del §6.6.
14. **Concurrencia.** El brief proponía `Lock` + `snapshot()`. Se sustituye por
    una `queue.Queue`: los callbacks de paho solo encolan `(topic, payload)`;
    el hilo principal drena la cola y hace el tick. Toda la lógica corre en un
    hilo → sin locks, más fácil de explicar en clase y de testear.
15. **Positions duplicadas** (gotcha §9.3): al indexar se prefiere la que está
    en el mapa activo del robot (`/status.map_id`); si sigue habiendo
    duplicado, la primera + warning.
16. **`busy`** = mission del FM (order o carga) viva **o** `state_id == 5`
    (Executing) sin mission del FM, es decir, una mission lanzada desde la web
    del MiR. Motivo: sin esto el asignador encolaría trabajo detrás de una
    mission manual y el ACK "ASSIGNED" sería engañoso.

### Bugs upstream en los schemas oficiales (tag 3.0.0)

- `order.schema`: coma sobrante tras `weight` (línea 314) → JSON inválido.
  Arreglado en `schemas/order.schema.json`.
- `factsheet.schema`: varias comas finales; y `typeSpecification.required`
  pide `mobileRobotKinematic` mientras la propiedad se llama
  `mobileRobotKinematics`. Arreglado en local; ver `schemas/README.md`.
- `dev/3.0.1` ya corrige el de `order`.

### Lo que dicen las missions de ejemplo (`ejemplos_mision/`, retirados del repo el 2026-09-17: eran referencia, no parte del proyecto)

| Mission | Inputs (`input_name`) | Notas |
|---|---|---|
| `Apertura Puerta H2DX` | `target_pos` (marker de docking) | PLC + relative_move |
| `Cerrar puerta H2DX` | `target_pos` (marker) | encadena `Ir a zona de espera` |
| `Ir a zona de espera` | — | 3 moves |
| `Montaje a paletizado` | `pieza` (número → registro PLC 3) | encadena zona de espera |
| `Recogida pieza H2DX - Pieza Y` | `target_pos` (marker), `pieza`, `sumergir` (0/1) | if/else sobre registro 9 |

Conclusiones para el adaptador:

- `target_pos` es el `input_name` de un `docking.marker`: el valor es el GUID
  de una **position de tipo marker**, no de una position normal. El índice
  `positions_get` incluye markers (mismo endpoint), así que basta con
  resolver por nombre sin filtrar `type_id`.
- Los inputs numéricos (`pieza`, `sumergir`) se envían tal cual como número.
- Los ejemplos vienen de un robot concreto: sus GUIDs **no** valen para el
  otro robot (§9.2). Solo se usan los nombres.
- No hay mission de carga en los ejemplos: pendiente de confirmar nombre con
  el usuario (y el modo "hasta % objetivo" para evitar el deadlock del §7).

## 2026-09-16 — H0: esqueleto, cliente REST y scripts

- `fm/config.py` acepta `AUTH_HEADER` como alias de `MIR_AUTH`: el `.env`
  del usuario viene del proyecto anterior con ese nombre y no merece la pena
  obligar a renombrarlo. `MIR_AUTH` tiene prioridad si están ambos.
- `fm/mir_client.py`: `MirClient` con los métodos `<recurso>_<verbo>` del
  brief, `MirApiError` que conserva el body JSON del 4xx, `MirStatus` con
  `theta` ya en radianes normalizados (`atan2(sin, cos)` evita el `%` con
  signos). Usa `requests.Session` para reutilizar la conexión a 1 Hz.
- `MirStatus` incluye `battery_time_remaining`, `mode_id` y
  `mission_queue_id` como candidatos a resolver `powerSupply.charging` (H4);
  `scripts/read_status.py` los imprime aparte para anotarlos con robot real.
- `scripts/_common.py` centraliza carga de config y cliente; `read_status.py`
  y `list_missions.py` marcan con `*` las missions referenciadas en
  `fleet.yaml` y avisan de las que no existen en ese robot.
- Tests offline en `tests/test_mir_client.py` (parseo, duplicados, error).
- **Pendiente hardware:** `192.168.15.5` y `192.168.15.15` no responden
  (timeout de conexión) desde este equipo. Sin verificar contra robot real:
  nombres de missions/positions de `fleet.yaml`, `input_name`s y campo que
  delata la carga. Se sigue con H1 (offline) hasta tener acceso.

## 2026-09-16 — H1: telemetría VDA (offline, robots inaccesibles)

- `fm/vda5050/header.py` centraliza topic ↔ header: `topic()`, `parse_topic()`,
  `make_header()` (con `HeaderCounter` monótono por topic) y
  `validate_header()` para lo entrante. Scripts y servicio usan lo mismo.
- `fm/vda5050/state.py`: dataclasses `State` + helpers; `to_dict()` omite
  opcionales `None` y conserva todas las listas `required` aunque vacías.
  Constantes `E_*` con los `errorType` v3 y los propios.
- `fm/vda5050/schemas.py`: validación con `jsonschema` Draft 2020-12 contra
  `schemas/`. Los tests validan todo `state` generado; `--validate` (H6) lo
  usará para lo entrante.
- `fm/adapters/mir.py::to_vda_state(header, status | None, StateOverlay)`.
  `StateOverlay` es el hueco para lo que H2/H4/H5 saben (order, actions,
  errores de rechazo, `charging`). Si `/status` falla se publica igual con
  `MIR_REST_UNREACHABLE` (URGENT) y sin `mobileRobotPosition`: mejor un state
  que dice "no sé dónde está" que silencio.
- `powerSupply.charging`: solo `True` cuando el FM sabe que su mission de
  carga está `Executing` (overlay). La heurística sobre `/status` sigue
  **pendiente de validar** con robot en el dock; `read_status.py` imprime los
  campos candidatos (`battery_time_remaining`, `mode_id`, `mission_queue_id`).
- `fm/mqtt_bus.py` (en vez de `mqtt_publisher.py` + tres suscriptores del
  brief: con la cola única de la decisión 14 basta un módulo). **Un cliente
  paho solo admite un Last Will** y la norma pide uno por robot → cliente
  principal (LWT de `fleet`, publica/recibe todo) + un cliente de presencia
  por serial (solo su LWT y su `connection`). Verificado con `kill -9`:
  el broker deja `CONNECTION_BROKEN` retained en `fleet`, `mir-1` y `mir-2`.
  Limitación asumida: el `headerId` del LWT se fija antes de conectar (vale 1)
  y el `ONLINE` posterior lleva 2, así que el `CONNECTION_BROKEN` llega con un
  `headerId` menor que el último `ONLINE`. No hay forma de evitarlo con LWT.
- `MirClient` usa `timeout=(2, 5)` (connect, read): un robot apagado costaba
  5 s por tick y frenaba la telemetría del otro.
- `fm/robot.py::Robot`: cliente + índices por robot (`refresh_indices`,
  tolerante: reintenta en el tick cuando el robot vuelve) + `poll()`.
- `run_fm.py`: bucle de un hilo, `--period`, `--robot` repetible, cierre
  limpio con SIGINT/SIGTERM (`OFFLINE` en todos los `connection`).
- `scripts/read_vda_state.py`: resumen por línea + validación de schema.
- Verificado contra Mosquitto local sin robots: `state` a 1 Hz por robot que
  valida contra el schema, `connection` ONLINE/OFFLINE/CONNECTION_BROKEN.
  **Pendiente con hardware:** posición/theta/mapId reales y `charging`.
- pytest: hay que lanzarlo con `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` porque el
  entorno tiene ROS Jazzy en `PYTHONPATH` y su plugin `launch_testing` falla
  al importar (`lark`). Añadido `pytest.ini` con `testpaths = tests`.

## 2026-09-16 — Reubicación en `fleet-manager-mirur/`

- Todo el FM (código, config, docs, schemas, PDF de la norma, ejemplos de
  missions y `.venv`) pasa a `mir-ur-2627/fleet-manager-mirur/`. En la raíz
  queda solo `MIR-UR-Node-red/` (otro repo). El venv se recreó (sus scripts
  llevan rutas absolutas). Todo se ejecuta desde esta carpeta.

## 2026-09-16 — Primer contacto con los robots reales

- **Grupo de missions.** El FM indexa solo el grupo `mirur-tknika`
  (`fleet.yaml: mission_group`): `GET /mission_groups` → GUID por robot →
  `GET /mission_groups/<guid>/missions`. Menos ruido y menos peticiones que
  `/missions` (los robots tienen 16 grupos). `index_missions_by_name(group)`.
- Missions del grupo en ambos robots (mismo nombre, GUID distinto):
  `coger`, `dejar` (sin inputs por ahora), `Carga en estación MIRUR`,
  `Ir a zona de espera MIRUR`. `fleet.yaml` reducido a `coger`/`dejar` y la
  carga apunta a `Carga en estación MIRUR`. Los parámetros llegarán después
  sin cambiar la estructura (`position_inputs`/`required_inputs` ya existen).
- **`/positions` no devuelve `map_id`**, solo `map` = `/v2.0.0/maps/<guid>`.
  `position_map_id()` lo extrae; sin esto la regla "preferir el mapa activo"
  no filtraba nada. Duplicados reales: `H2D1-VL`/`H2D2-VL` con `type_id`
  11 y 12 (marker VL y su posición de entrada) y `Charging station 48V 35A`
  con 20/21, todos en el mapa activo → se queda con el primero (tipo menor).
- `scripts/run_mission.py <serial> <mission> [k=v…]`: POST + seguimiento de
  `mission_queue` hasta terminar. Verificado: `coger` en mir-1 → `Done` en
  82 s (H2D1 → espera 5 s → Entrada zona espera → Punto espera 2). `dejar`
  en mir-2 → `Aborted` a los 161 s **porque el usuario la paró desde la web**
  (el destino final `Punto espera 2` estaba ocupado por mir-1 y el robot
  esperaba). Confirma que un abort manual llega como `Aborted` en la cola,
  igual que un fallo: el FM no distingue el motivo (→ `ORDER_EXECUTION_FAILED`).
- `/status` observado: `mode_id=7 (Mission)`, `mission_queue_id=None` en
  Ready; `mission_text` lleva a veces HTML (`<p>Moving to 'H2D2'</p>`) →
  se pasa tal cual a `information[]`. `battery_time_remaining` a 76 % =
  52660 s (descargando). Sigue pendiente ver qué cambia en el dock para
  `powerSupply.charging`.
- La API responde en < 100 ms en LAN; el tick a 1 Hz con 2 robots es holgado.

## 2026-09-16 — H2 + H3: order dirigida, asignador y `fleet/order` (probado con robots)

Implementado de golpe porque la prueba pedida ("mir-2 ocupado, mando una
order sin asignar y la hace mir-1") necesita ambos hitos.

### Código

- `fm/vda5050/order.py`: `Order/Node/Action/ActionParameter`, `parse_order()`
  con la validación estructural de la decisión 10 y `OrderRejected(error_type,
  description)` como excepción única de rechazo.
- `fm/adapters/mir.py`: `pick_action()` (una sola action pendiente entre los
  nodos released) y `from_vda_order()` (Action + índices del robot →
  `MissionRequest`). Puras, con tests.
- `fm/orders.py::OrderTracker`: una order activa por robot, `check_new()` con
  el ciclo `orderUpdateId` (§6.1.4), `poll()` que traduce `mission_queue.state`
  a `actionStatus`, `overlay()` → `StateOverlay`. Listas separadas de errores
  de rechazo / de ejecución (decisión 2).
- `fm/assigner.py::assign()`: pura; filtros soporta → disponible → batería →
  ocupado → (prefer_not_charging); ranking mayor batería, alfabético.
- `fm/fleet.py::Dispatcher`: drena `bus.inbox` en el hilo principal;
  `<serial>/order` → tracker del robot; `fleet/order` → asignador → POST en
  el elegido → `order_response`. Publica `state` inmediatamente tras
  aceptar/rechazar (decisión 13). Idempotencia de `fleet/order` por
  `(orderId, orderUpdateId)` del último aceptado: se ignora sin respuesta.
- `fm/robot.py`: `busy`, `foreign_mission`, `snapshot()`, `translate_order()`,
  `execute()`.
- Scripts: `send_order.py` (dirigida), `send_fleet_order.py` (flota, espera
  `order_response`), `_mqtt.py` (construye el header con `fm.vda5050.header`).

### Decisiones nuevas

17. **`actionType` no mapeado → `INVALID_ORDER_ACTION`** (tabla 9 v3, "action
    desconocida"), no `NO_ROUTE_TO_TARGET`, que queda para position inexistente,
    fuera de allowlist o mission ausente en ese robot.
18. **Ocupado** (amplía la decisión 16): además de mission del FM viva o
    `state_id == 5`, cuenta `/status.mission_queue_id != None`. Motivo: mir-2
    estaba en **Pause (4)** con una mission de la web en cola y sin este campo
    el asignador lo habría considerado libre.
19. Una `fleet/order` ya asignada que se repite se ignora **sin**
    `order_response` (idempotencia = silencio, igual que en `<serial>/order`).
    Si el emisor pierde el ACK, debe reenviar con otro `orderId` o consultar
    `state.orderId` de los robots.
20. **Ambos robots ocupados → rechazo, sin cola interna.** `fleet/order` →
    `REJECTED NO_MOBILE_ROBOT_AVAILABLE` con el detalle por robot, y el FM no
    guarda ni reintenta la order; reencolar es responsabilidad del sistema
    aguas arriba. Un reenvío posterior con el mismo `orderId` se acepta si hay
    robot libre (la idempotencia solo cubre la última order aceptada).
    Confirmado con el usuario el 2026-09-16: "por ahora se queda así". Si algún
    día hace falta cola FIFO en el FM, sería una lista en `Dispatcher` revisada
    por tick + `order_response` con `status: "QUEUED"`, cambiando el contrato.
21. El robot elegido por el asignador puede aún rechazar (p.ej. la mission
    no existe en él): ese rechazo sale como `REJECTED` con su `errorType`; no
    se reintenta con el siguiente candidato en v1.

### Prueba con robots (13:25–13:27)

Escenario: mir-2 en Pause con mission de la web (`queue_id=337`); mir-1 Ready 74.9 %.

1. `send_fleet_order.py coger` → `ASSIGNED → mir-1`; mission `coger` id=1311;
   `state` de mir-1 con `orderId=fleet-3928d0aa`, `acts=coger:RUNNING`.
2. Segunda `fleet/order coger` con ambos ocupados → `REJECTED
   NO_MOBILE_ROBOT_AVAILABLE: mir-1: ocupado; mir-2: ocupado`.
3. Misma order repetida → ignorada (log), sin `order_response`.
4. Cola `Executing → Done` a los 89 s → `acts=coger:FINISHED`, `orderId` se
   conserva en `state`. Ctrl-C → `OFFLINE` en los tres `connection`.

### Pendiente de probar

- **Con hardware:** `<serial>/order` dirigida (`send_order.py`); order con
  parámetros (`position_inputs`/`required_inputs`) cuando `coger`/`dejar` los
  tengan; `Aborted` desde la web durante una order del FM →
  `ORDER_EXECUTION_FAILED`; header ≠ topic → `REJECTED VALIDATION_FAILURE`
  (probado solo en tests); order update (`orderUpdateId > 0`) sobre order
  terminada; `powerSupply.charging` en el dock.
- **Sin hacer:** H4 auto-carga (`charge.py`), H5 instantActions (`startPause`,
  `stopPause`, `cancelOrder`, `stateRequest`, `clearInstantActions`,
  `factsheetRequest`), H6 (`--validate` con schemas en runtime, `factsheet`
  retained, smoke completo del §13).
- Duplicados `*-VL` con `type_id` 11/12 en el mapa activo: hoy se elige el
  primero (11). Confirmar con el usuario cuál debe recibir `target_pos`
  cuando lleguen los parámetros.

## 2026-09-17 — Drivers enchufables, fase 1: tipos y frontera

Plan completo en `docs/plan-drivers.md` (aprobado ese mismo día). Esta fase
formaliza la frontera core ↔ marca sin cambiar comportamiento: `pytest` en
verde (34 tests) y el flujo con los MiR idéntico.

### Código

- `fm/adapters/base.py` (nuevo): `Telemetry`, `Job`, `JobStatus` y el
  `Protocol` `RobotDriver`. Es lo único que el core conocerá de un robot.
- `fm/vda5050/state_builder.py` (nuevo): `StateOverlay` y `to_vda_state`
  pasan al core y reciben `Telemetry`, no `MirStatus`. Un driver ya no puede
  publicar un `state` mal formado: solo aporta telemetría.
- `fm/adapters/mir.py`: solo traducción MiR. Nueva `to_telemetry(MirStatus,
  own_queue_id)` (absorbe `_mir_errors`, `operating_mode`, `foreign_busy`);
  se va `to_vda_state`. `pick_action` se muda a `fm/orders.py` (es regla VDA).
- `OrderTracker.check_new(order, telemetry)` usa `telemetry.available` y
  `unavailable_reason`; ya no mira `state_id`.
- `Robot`: `poll()` devuelve `Telemetry` y guarda `last_telemetry`;
  `busy`/`available`/`snapshot` leen de ahí. Aún crea `MirClient` y
  mantiene los índices (fase 2).
- `run_fm.py` ya no importa nada de `fm.adapters.mir`; el log del tick imprime
  `mode=`/`drv=` en vez de `state_id`.
- Tests: `tests/adapters/test_mir.py` (MirStatus → Telemetry);
  `test_vda_state.py` pasa por `to_telemetry` y añade un caso de telemetría
  mínima sin pose (lo que publicará un driver `sim`).

### Decisiones nuevas

22. **`MIR_REST_UNREACHABLE` → `ROBOT_UNREACHABLE`** (`E_ROBOT_UNREACHABLE`).
    El error lo genera el core cuando `driver.poll()` devuelve None; con
    varias marcas no puede llevar "MIR" en el nombre. Es un errorType propio
    (no de la norma), así que solo afecta a quien lo filtre aguas arriba.
23. **`Telemetry.charging` es `bool | None`**: None = el driver no lo sabe y
    manda el overlay (auto-carga del FM). El MiR devuelve None hasta validar
    la heurística en el dock (§5.2). Prioridad en `to_vda_state`: overlay →
    driver → False.
24. **`foreign_busy` lo calcula el driver**, no el core: la traducción MiR
    recibe `own_queue_id` (la entrada de `mission_queue` que lanzó el FM) y
    marca ajena cualquier otra mission en marcha o en Pause (decisiones 16/18).

### Nota de entorno

`pytest` en este WSL carga plugins de ROS Jazzy del site-packages del sistema
y falla al importar `lark`. Ejecutar con
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest`.

## 2026-09-17 — Drivers fase 2: `MirDriver`

El core deja de importar nada de MiR. `pytest`: 39 en verde.

### Código

- `fm/adapters/mir/` (paquete): `client.py` (= antiguo `mir_client.py`, sin
  cambios), `translate.py` (= antiguo `adapters/mir.py`: `to_telemetry`,
  `from_vda_order`, `MissionRequest`), `driver.py` (`MirDriver`).
  `fm/mir_client.py` queda como shim porque `scripts/` importan de ahí.
- `MirDriver` encapsula lo que antes repartían `Robot` y `OrderTracker`:
  índices nombre → GUID (`connect()`), `GET /status` → `Telemetry`
  (`poll()`), `POST /mission_queue` (`execute()` → `job_id` = id de cola como
  str), `GET /mission_queue/{id}` → `JobStatus` (`job_status()`),
  `DELETE` (`cancel()`), y `charge_job()` para H4.
- `OrderTracker` ya no toca red: `ActiveOrder(order, job, job_id,
  job_status)`; `poll(driver)` pregunta `driver.job_status()` y conserva el
  último estado si devuelve None. `QUEUE_TO_ACTION` se va al driver
  (`QUEUE_TO_JOB`).
- `Robot(cfg, driver)`: composición driver + tracker + `last_telemetry`.
  `connect()` reintentado desde `poll()` mientras falle; `translate_order`
  y `execute` delegan en el driver. ~90 líneas, sin imports de marca.
- `run_fm.py`: construye `MirDriver` explícitamente (la fase 3 lo sustituye
  por `make_driver()`); `tick_robot` ya no conoce `cfg`.
- Tests: `tests/adapters/test_mir_driver.py` (cliente falso: connect →
  translate → execute → job_status → cancel, propia vs ajena, charge_job);
  `test_mir_client.py` se mueve a `tests/adapters/`; `test_orders.py` usa
  `FakeDriver` con `JobStatus` en vez de `FakeClient` con estados de cola.

### Decisiones nuevas

25. **Missions propias = conjunto de ids de cola lanzados por el driver**
    (`_own_queue_ids`); se retira un id al verlo FINISHED/FAILED. Una
    mission en marcha cuyo id no esté ahí es ajena (web) → `foreign_busy`.
    Antes se deducía "ajena = hay mission y el tracker no está ocupado", que
    es equivalente hoy pero no permitiría distinguir la mission de auto-carga
    (H4) de una lanzada desde la web.
26. **`job_id` es `str` opaco** en la frontera (el MiR usa el int de la cola,
    otra marca puede usar un UUID). El driver convierte.

## 2026-09-17 — Drivers fase 3: configuración y registro

`pytest`: 45 en verde. `run_fm.py` arranca con el nuevo `fleet.yaml` y
construye los dos `MirDriver` por el registro.

### Código

- `fm/config.py`: `RobotConfig(serial, driver, manufacturer, battery_min,
  action_types, raw)`. El core solo interpreta eso; `raw` es el bloque
  completo para el driver. `AutoChargeConfig` pierde `mission`;
  `FleetConfig` pierde `mission_group`/`positions_allowlist` y gana
  `drivers: dict` (+ `driver_defaults(name)`). `ConfigError` para fallos
  de configuración; `run_fm` los imprime y sale con 2.
- `fm/adapters/mir/config.py`: `ActionConfig` (movido desde `fm/config.py`),
  `MirRobotConfig` y `parse_config(serial, raw, defaults, env)`: fusiona
  `drivers.mir` ← `robots.<s>` y resuelve `MIR_HOST_*`/`MIR_AUTH_*`.
- `fm/adapters/__init__.py`: `DRIVERS = {"mir": "fm.adapters.mir"}` y
  `make_driver(rcfg, fleet, env)`. Cada paquete de driver expone
  `make_driver(serial, raw, defaults, env)`. Import perezoso por nombre de
  módulo: el core no carga marcas que no use.
- `config/fleet.yaml` en el formato nuevo; `README` sección de configuración.
- `scripts/_common.py`: `robot_or_exit` devuelve `MirRobotConfig` (los
  scripts son de MiR) y rechaza serials con otro driver.
- `tests/test_config.py`: carga genérica, fusión de defaults/entorno,
  registro, y los tres errores "clave en el sitio antiguo".

### Decisiones nuevas

27. **`actions:` es convención para todos los drivers**: mapping
    `actionType → config de la marca`. El core solo lee sus claves
    (`RobotConfig.action_types`) para que el asignador sepa qué robots
    soportan el `actionType` de la order sin conocer la marca.
28. **Claves antiguas en la raíz → error al arrancar**, no aviso: como
    acordamos, sin capa de compatibilidad, pero el mensaje dice dónde va
    ahora cada clave (`mission_group` → `drivers.mir.mission_group`, etc.).
29. **`robots.<s>.manufacturer` pisa el del driver** (`make_driver` lo asigna
    en la instancia). Sirve para la fase 4 (manufacturer por robot en el
    topic) y para marcas cuyo nombre en el topic no es el de la clase.

## 2026-09-17 — Drivers fase 4: manufacturer por robot

`pytest`: 50 en verde. Contrato MQTT: cambia el topic y el header de
`fleet/*`; los de los MiR no cambian (`vda5050/v3/MiR/<serial>/…`). No había
ningún flujo Node-RED que actualizar (confirmado con el usuario).

### Código

- `MqttConfig.manufacturer` → `MqttConfig.fleet_manufacturer`
  (`imperial_fleet` por defecto). `mqtt.manufacturer` en el yaml → `ConfigError`
  con la pista.
- `MqttBus(host, port, {serial: manufacturer}, fleet_manufacturer)`:
  `topic()`, `next_header()`, Last Will y `publish_raw()` usan el manufacturer
  de cada serial; `is_known(manufacturer, serial)`.
- `Dispatcher.subscribe()`: `vda5050/v3/+/+/order` y `+/+/instantActions`;
  `handle()` descarta (DEBUG) lo que no sea un `(manufacturer, serial)`
  configurado. `validate_header` no cambia: header ≡ topic sigue igual.
- `run_fm.py` pasa `{s: robots[s].manufacturer}` (el del driver, o
  `robots.<s>.manufacturer`).
- Scripts: `send_fleet_order` usa `fleet_manufacturer`; `send_order` y
  `read_vda_state` resuelven el manufacturer del robot vía `make_driver`
  (`_common.manufacturer_for`); `read_vda_state` sin serial escucha `+/+`.
- Tests: `tests/test_fleet.py` (Dispatcher con bus y drivers falsos: filtro
  por manufacturer, `fleet/*` bajo `imperial_fleet`, header ≠ topic) y
  `tests/test_mqtt_bus.py` (topics/headers sin broker).

### Decisiones nuevas

30. **Manufacturer por robot y `imperial_fleet` para `fleet/*`** (cerrada en
    el plan, aplicada aquí). Norma §6.2: `manufacturer` = fabricante del robot,
    así que `vda5050/v3/MiR/mir-1/…` y, mañana, `vda5050/v3/OMRON/ld-1/…`.
    `fleet` no es un robot: su segmento es un nombre de flota propio para que
    no parezca que la flota "es de MiR". Emisor de `fleet/order`: topic
    `vda5050/v3/imperial_fleet/fleet/order`, header `manufacturer:
    "imperial_fleet", serialNumber: "fleet"`.
31. **Suscripción con comodín `+/+` y filtro en `handle()`** en vez de una
    suscripción por robot: menos suscripciones, y un mensaje bajo un
    manufacturer equivocado no genera rechazo (nadie lo escucharía) sino un
    log DEBUG. Un mismo serial bajo dos manufacturers no colisiona porque
    `HeaderCounter` y `is_known` indexan por el par.

## 2026-09-17 — Drivers fase 5: driver `sim` y tests de contrato

`pytest`: 57 en verde. Probado end-to-end contra el Mosquitto local con
`config/fleet-sim.yaml` (sin robots): `fleet/order coger` → ASSIGNED sim-1;
`dejar` → ASSIGNED sim-2; tercera con ambos ocupados → `REJECTED
NO_MOBILE_ROBOT_AVAILABLE`; a los 8 s `coger:FINISHED` en sim-1 y
`dejar:FAILED` + `ORDER_EXECUTION_FAILED` en sim-2 (`fail_actions`); la
batería baja mientras ejecutan; `state` bajo `vda5050/v3/SIM/sim-1/…`.

### Código

- `fm/adapters/sim/`: `SimDriver` + `SimRobotConfig` + `parse_config`.
  Sin red: los jobs terminan por reloj (`duration_s`), inyectable en tests;
  `drain_pct_per_s` descarga (o carga, si el job es de carga);
  `fail_actions` fuerza FAILED; `cancel()` termina el job en FAILED;
  `charge_job()` siempre disponible; sin `pose` no publica posición.
  Registrado como `"sim"` en `fm/adapters/__init__.py`.
- `config/fleet-sim.yaml`: dos sims; `fleet.yaml` lleva un bloque `sim-1`
  comentado para mezclar con los reales en clase.
- `tests/adapters/test_contract.py`: parametrizado `[sim, mir]`, es la
  definición ejecutable de lo que el core exige a un driver.
- `tests/adapters/test_sim_driver.py`: reloj, batería, fallos, registro.

### Decisiones nuevas

32. **El `sim` publica bajo manufacturer `SIM`**, no `MiR`: demuestra la
    fase 4 (varios manufacturers en el mismo bus) y evita que un sim se
    confunda con un robot real en `mosquitto_sub -t 'vda5050/#'`.
33. **`Job.payload` del sim es un dict** (`duration_s`, `charge`): el core
    no lo mira, y así `charge_job()` y `translate()` comparten `execute()`.
    Confirma que `payload` opaco basta para dos marcas muy distintas.
