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
- `factsheet.schema`: `mobileRobotActions[].pauseAllowed`/`cancelAllowed`
  tipados como `string`; el PDF (§7.10, p. 97) dice `boolean`. Parcheado en
  local (2026-09-18).
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
ningún emisor aguas arriba que actualizar (confirmado con el usuario).

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

## 2026-09-17 — Drivers fase 6a: documentación

- `README.md`: sección "Añadir una marca (driver)" con la tabla del
  `Protocol`, los cuatro pasos (carpeta, `make_driver`, registro, contrato) y
  la nota de que el `state` lo construye siempre el core.
- `CLAUDE.md` §4 (diagrama, estructura real, principios), §5 (manufacturer
  por robot, `imperial_fleet`), §5.6/5.7 (headers de `fleet/*`) y §8
  (`fleet.yaml` en formato nuevo). El resto del brief se deja como estaba:
  es el documento de arranque y su historia sigue siendo válida.
- Fase 6b (validación con robots): ver la entrada siguiente.

## 2026-09-17/18 — Drivers fase 6b: validación con los MiR reales

Cierra el plan de drivers: el FM refactorizado se comporta con los dos
MiR250 igual que el 2026-09-16, y además se han probado casos que entonces
quedaron pendientes. `pytest`: 57 en verde.

### Código (ajustes a raíz de la prueba)

- `MirDriver.job_status`: `'Abort'` (y `'Cancel'`) son estados transitorios
  de `mission_queue` vistos en robot real (un tick antes de `'Aborted'`);
  ahora devuelven None sin warning ("estado desconocido") y el core conserva
  el último estado hasta el tick siguiente.
- `to_telemetry` añade una `Info("MIR_STATUS", "DEBUG", "state_id=… mission_queue_id=… (propia)")`
  (nivel `DEBUG` es válido en el schema §7.8). Sirve para explicar un
  "ocupado" sin abrir la web del MiR; el log del tick la imprime junto con
  `busy=<propia>/<ajena>`.

### Prueba 2026-09-17 12:43–12:49

mir-1 Ready 49.9 %, mir-2 Ready 36.6 %, ambos sin mission.

1. Arranque: índices 4 missions / 21 y 20 positions (mismos avisos de
   duplicados que el 16/09); `state` bajo `vda5050/v3/MiR/<serial>/state`.
2. `fleet/order coger` → `ASSIGNED mir-1` (mayor batería), job 1314 `RUNNING`.
   Segunda → `ASSIGNED mir-2`, job 338 `RUNNING`.
3. Tercera con ambos ocupados → `REJECTED NO_MOBILE_ROBOT_AVAILABLE: mir-1:
   ocupado; mir-2: ocupado`. Reenvío con el `orderId` de la primera →
   también rechazada (la idempotencia solo cubre la última order aceptada,
   decisión 20).
4. mir-2 abortada desde la web a los 27 s → `coger:FAILED` +
   `ORDER_EXECUTION_FAILED` en `state.errors`; mir-1 abortada a los 3.5 min.
5. Robots en **Pause** sin mission en cola → `fleet/order` aceptada
   (`ASSIGNED mir-1`, job 1315 `WAITING` en la cola hasta reanudar). Es lo
   previsto en las decisiones 16/18: Pause solo bloquea si hay mission ajena.
6. Un rechazo "mir-2: ocupado" a las 12:46:54 con mir-2 aparentemente Ready
   resultó ser una mission ajena en cola (mir-2 alternó Executing/Ready esos
   minutos); de ahí la `Info MIR_STATUS` de arriba.

### Prueba 2026-09-18 08:04–08:13

7. Ctrl-C → `OFFLINE` retenido en `mir-1`, `mir-2` y `fleet/connection`
   (`imperial_fleet`).
8. `fleet/order coger` → mir-1, `RUNNING → FINISHED` a los 2 min 41 s
   (primer `Done` real con el código nuevo); `orderId` se conserva en `state`.
9. **Mission ajena**: con una mission lanzada desde la web en mir-1
   (`busy=0/1`, queue 1319 no propia): `fleet/order` → `ASSIGNED mir-2`
   pese a tener menos batería; segunda → `REJECTED` (mir-1 ajena, mir-2
   propia); order **dirigida** a mir-1 → `MOBILE_ROBOT_NOT_AVAILABLE: el
   robot ejecuta un trabajo ajeno al FM`, visible en `state.errors`.
10. **Robot apagado** (mir-1): `state` sigue publicándose sin posición y con
    `ROBOT_UNREACHABLE` (URGENT, `ConnectTimeout`); `fleet/order` →
    `ASSIGNED mir-2` (mir-1 `available=False`); `GET /status` se reintenta
    cada tick.

### Observaciones / pendiente

- **Cadencia con un robot apagado**: cada tick espera el timeout de
  conexión (2 s) del robot caído, así que el `state` de TODOS baja de 1 Hz a
  ~0,5 Hz. No es de esta refactorización (bucle secuencial de un hilo).
  Solución propuesta: `poll()` de los robots en paralelo con un
  `ThreadPoolExecutor` solo para esa llamada. Pendiente de decidir.
- No probado aún: recuperación de un robot que vuelve a encenderse
  (índices ya cargados; debería bastar con que `poll()` vuelva a responder),
  order con parámetros (`position_inputs`), order update sobre order
  terminada, `powerSupply.charging` en el dock.
- H4 auto-carga sigue sin implementar: `charge_mission` se resuelve y
  `driver.charge_job()` existe, pero nada lo llama todavía.

## 2026-09-18 — H4: auto-carga

`pytest`: 62 en verde. Probado end-to-end con `config/fleet-sim.yaml` contra
el Mosquitto local (sim-2 arranca al 27 %, una `coger` lo deja al 19.9 % →
carga encolada detrás de la order `WAITING` → `RUNNING` con `chg=1` y
`busy=0/1/0` → las orders siguientes van a sim-1 → `FINISHED` al 39 % →
sim-2 vuelve a ser asignable). **No probado aún con los MiR reales**
(requiere que un robot baje del 20 % o bajar el suelo en `fleet.yaml`).

### Código

- `fm/charge.py`: `ChargeGuard` por robot. `tick(driver, telemetry)`: si
  hay carga activa la sigue con `driver.job_status()`; si no, con batería
  bajo el suelo, robot disponible y fuera de cooldown, pide
  `driver.charge_job()` y lo ejecuta con `priority`. `decorate(overlay)`
  añade `AUTO_CHARGE` a `information[]` y `charging=True` si el job está
  RUNNING.
- `Robot`: `charge: ChargeGuard`; `busy` incluye `charge.active`;
  `overlay()` = orders + carga; `snapshot().charging` = carga del FM en
  ejecución o `telemetry.charging` del driver. `run_fm.py` pasa
  `cfg.auto_charge` y el log del tick imprime `chg=` y `busy=order/carga/ajena`.
- `SimDriver`: cola secuencial (un job arranca cuando termina el anterior,
  como en un robot real) y batería integrada por solape de cada job con el
  intervalo entre polls. Antes los jobs corrían en paralelo y la batería
  se atribuía al job "actual" en el momento del poll, lo que daba cifras
  incoherentes en los tests de carga.
- `config/fleet-sim.yaml`: sim-2 al 27 % y `drain_pct_per_s: 1.0` para
  ver la auto-carga en una demo de un minuto.
- Tests: `tests/test_charge.py` (guard con driver falso: suelo, latch,
  cooldown tras FAILED y tras error de red, driver sin carga; end-to-end
  con el sim incluyendo `state` válido contra el schema y el asignador).

### Decisiones nuevas

34. **La auto-carga no comprueba `foreign_busy`**: si el robot ejecuta una
    mission de la web, la carga se encola detrás igual que detrás de una
    order del FM ("no aborta nada", §7). Sí exige `available` (no Manual /
    Error / E-stop) y telemetría.
35. **`charging` se aproxima con "job de carga RUNNING"**, que incluye el
    docking y la salida del dock. La heurística sobre `/status` sigue
    pendiente (§5.2); cuando exista, `Telemetry.charging` del driver la
    aportará y `to_vda_state` ya la combina (overlay → driver → False).
36. **Error al postear la carga → mismo cooldown que un FAILED**: evita
    martillear un robot sin red o que rechaza la mission (p.ej. GUID
    inexistente) a 1 Hz.
37. **Mission de carga del MiR verificada** (2026-09-18, mir-2):
    docking → charging (`minimum_percentage: 70`, `charge_until_new_mission:
    false`) → relative_move → if/load_mission. Termina `Done` sola: se elige
    la opción "porcentaje objetivo" de la trampa del deadlock (§7).

### Prueba de auto-carga con mir-2 real (2026-09-18 08:37–08:49)

`battery_floor` subido temporalmente a 32 en `fleet.yaml` (mir-2 al 30.5 %,
mir-1 apagado, FM con `--robot mir-2`):

1. Primer tick: `batería 30.5% < suelo 32%: auto-carga mission 'Carga en
   estación MIRUR' job=347 (prioridad 10)` → `WAITING → RUNNING`; el robot
   va al dock (`drv=1`, `chg=1`, `busy=0/1/0`, cola 347 "propia").
2. `state`: `powerSupply.charging: true`, `information[]` con `AUTO_CHARGE
   "mission 'Carga en estación MIRUR' (RUNNING)"` y `MISSION "Moving to
   'Charging station 48V 35A' (0 meters to goal)"`.
3. `fleet/order coger` mientras carga → `REJECTED NO_MOBILE_ROBOT_AVAILABLE:
   mir-2: ocupado`.
4. Carga abortada desde la web a los 10 min → `RUNNING → FAILED`, cooldown
   60 s, `chg=0`, robot libre. Batería 30.5 → 52.4 % en el dock: el
   `charging=true` aproximado (decisión 35) coincidió con la realidad.
5. `battery_floor` devuelto a 20.

Nota de uso: los valores de `AutoChargeConfig` en `fm/config.py` son solo
defaults de Python por si `fleet.yaml` no trae la clave; el yaml manda.

## 2026-09-18 — H5: instantActions

`pytest`: 67 en verde. Probado con el sim contra el broker y con **mir-2
real** (09:09): `send_order.py mir-2 coger` → `startPause` → el MiR pasa a
`state_id=4 (Pause)` a 6.8 m del objetivo, la mission sigue en cola
(`coger:RUNNING`, `busy=1/0/0`) → `stopPause` → `state_id=5`, reanuda →
`cancelOrder` + `stateRequest` → `DELETE /mission_queue/349`, robot Ready sin
mission, `coger:FAILED` sin errores, `instantActionStates` con los dos
`FINISHED`. mir-1 no participó (en uso para otra cosa).

### Código

- `fm/vda5050/order.py`: `parse_action` (compartido) y
  `parse_instant_actions(dict) -> list[Action]`.
- `fm/adapters/base.py`: `pause()` / `resume()` en el `Protocol`.
  `MirDriver`: `PUT /status 4|3`. `SimDriver`: reloj "de trabajo" `_now()`
  que no avanza en pausa (los jobs se congelan; `paused=True`,
  `driving=False` en la telemetría).
- `fm/instant.py`: `apply_instant_actions(robot, actions, publish_state)`.
  Cada action → `FINISHED`/`FAILED` en `tracker.instant_states`; fallos en
  `tracker.instant_errors` con `errorReferences` a `actionId` (y `orderId`
  si hay order activa). Excepciones del driver (red, 4xx) → `FAILED` +
  `INVALID_INSTANT_ACTION` con el mensaje.
- `OrderTracker.cancel()`: la order activa pasa a `FAILED` sin
  `exec_errors`.
- `Dispatcher.handle_instant_actions`: header ≠ topic o JSON sin `actions`
  → `VALIDATION_FAILURE` suelto en `errors[]` + `state` inmediato.
- `scripts/send_instant_action.py <serial> <actionType>…`.
- Tests: `tests/test_instant.py` (unidad con driver falso, Dispatcher, sim
  en pausa); contrato ampliado con `pause()/resume()`; `_FakeClient` del
  MiR con `PUT /status`.

### Decisiones nuevas

38. **`cancelOrder` no es un fallo de ejecución**: la action de la order
    queda `FAILED` (la norma no tiene "CANCELLED" en `actionStatus`) pero no
    se añade `ORDER_EXECUTION_FAILED` a `errors[]`; el brief §5.5 decía
    "con orderError", se descarta porque el operador pidió la cancelación.
    `orderId` se conserva en el `state` hasta la siguiente order (§6.6).
39. **`instantActionStates` y sus errores se conservan hasta el siguiente
    mensaje `instantActions`**, no se limpian al aceptar una order (a
    diferencia de los errores de rechazo de order, decisión 2). Así un
    `NO_ORDER_TO_CANCEL` sigue visible aunque después llegue una order.
40. **Driver sin `pause()`/`resume()`** (atributo ausente o None) →
    `FAILED` + `INVALID_INSTANT_ACTION "el driver X no soporta 'pause'"`,
    sin excepción: una marca puede no implementar la pausa y el FM sigue.
41. **`factsheetRequest` → FAILED**: `factsheet` sigue fuera de alcance
    (§1); si se implementa, aquí es donde se publicaría retained.

## 2026-09-18 — Poll en paralelo con backoff (cadencia con robot apagado)

`pytest`: 68 en verde. Medido con `sim-1` + un "MiR" en una IP inexistente
(timeout de conexión 2–3 s): antes, el `state` de sim-1 salía cada 2–3 s;
ahora cada 1.00 s sin excepción, y el robot caído se consulta cada 8 s.

### Código

- `Robot.poll()`: backoff exponencial tras fallos consecutivos (1, 2, 4, 8,
  8… s, `BACKOFF_MAX_S`); entre intentos devuelve None sin tocar la red y el
  `state` sigue publicándose con `ROBOT_UNREACHABLE`. Log "sin respuesta (N
  seguidos): próximo intento en Xs" y "vuelve a responder tras N fallos".
  `clock` inyectable (tests).
- `run_fm.py`: `ThreadPoolExecutor` (un hilo por robot) para `Robot.poll()`.
  `poll_all(grace)`: lanza el poll de cada robot que no tenga uno pendiente,
  espera como mucho `grace = min(0.5, period/2)` s **solo a los robots que
  respondían** (los que están en backoff no retrasan nada), y recoge los
  terminados. Un poll que tarda más sigue en su hilo; ese robot publica su
  último estado conocido y se actualiza en el tick en que vuelva.
  `tick_robot()` ya no llama a `poll()`: solo construye y publica.
- `tests/test_robot_backoff.py`.

### Decisiones nuevas

42. **Paralelismo acotado a `Robot.poll()`**. Cada hilo toca solo el estado
    de su robot (telemetría, tracker, auto-carga); el hilo principal lee ese
    estado para publicar y para despachar orders. No hay locks porque:
    (a) las orders a un robot solo llegan al driver si `last_telemetry` no
    es None, y un robot con poll pendiente está inalcanzable (None) o es un
    poll normal de ~50 ms que `grace` ya ha esperado; (b) las asignaciones
    de atributos son atómicas bajo el GIL y `overlay()` copia las listas.
    Si algún día el poll de un robot alcanzable pudiera superar `grace` de
    forma habitual (Wi-Fi malo), habría que revisar (a).
43. **Un poll pendiente no se apila**: si el anterior no ha vuelto, no se
    lanza otro; así un robot apagado consume como mucho un hilo y una
    conexión en curso.

## 2026-09-18 — Orders con parámetros probadas con mir-2

El usuario añadió al grupo `mirur-tknika` tres missions de prueba (copiadas
de otro proyecto; las definitivas, tipo "coge 5 contactores y llévalos a la
balda 3", están por desarrollar): `Ir a zona de espera MIRUR` (sin inputs),
`Simular Apertura Puerta H2DX MIRUR` y `Simulacion Recogida pieza H2DX
MIRUR` (input `target_pos`, position; esta última solo en mir-2).

`fleet.yaml`: actions `ir_a_espera`, `abrir_puerta` y `recoger_pieza`
(solo mir-2) con `position_inputs: [target_pos]`.

Prueba (10:00, FM con `--robot mir-2`):

1. `send_order.py mir-2 abrir_puerta` sin parámetro → `VALIDATION_FAILURE:
   falta el parámetro obligatorio 'target_pos' (position)`.
2. `target_pos=NoExiste` → `NO_ROUTE_TO_TARGET: [mir-2] position 'NoExiste'
   no existe`.
3. `target_pos=H2D2` → aceptada; en la cola del MiR (id 350)
   `parameters: [{"id": "target_pos", "value": "c2625f33-…", "label": "H2D2"}]`
   y el robot fue a H2D2 ("Moving to 'H2D2' (3.7 meters to goal)").
   Abortada desde la web (la mission espera un registro PLC) → `FAILED`.

Con esto quedan probados con robot todos los caminos de `from_vda_order`
salvo `required_inputs` (no hay mission con inputs no-position) y la
`positions_allowlist` (cubiertos por tests). Lo de las positions `*-VL`
"duplicadas" se cierra en la decisión 48.

## 2026-09-18 — Interfaz web (docs/plan-ui.md)

`pytest`: 71 en verde. Probada con el sim y con los MiR reales (12:13–12:18):
desde el navegador `fleet/order abrir_puerta target_pos=H2D1-VL` → asignada a
mir-2 (mir-1 en E-stop, tarjeta roja "parada de emergencia"), mission en
cola y `RUNNING`; ✖ → `cancelOrder` FINISHED y mission abortada; `dejar` +
⏸/▶ tres veces, cada `startPause`/`stopPause` FINISHED y reflejado en la
tarjeta ("en pausa") en ~1 s.

### Código

- `fm/web/server.py` (FastAPI + uvicorn en hilo propio): `GET /` y
  `/static`, `GET /api/fleet` (robots, actions con parámetros y opciones,
  umbrales), `WS /ws` (al conectar, último `state`/`connection` de cada
  robot; después cada publicación del FM `{topic, payload}`),
  `POST /api/fleet/order` y `POST /api/robots/{serial}/instant` (publican en
  el broker con header VDA). `--web-port` (8050; 8080 lo ocupa Airflow en
  el PC del taller). Si el puerto está ocupado, log de error y el FM sigue
  sin web.
- `MqttBus.on_publish`: gancho espejo, instalado en `WebServer.__init__`
  antes de `bus.start()` para capturar los `connection` ONLINE del arranque.
  `HeaderCounter` con `Lock` (lo usan dos hilos).
- `RobotDriver.describe_actions()` (opcional) → `ActionInfo`/`ParamInfo`;
  MiR: actions de `fleet.yaml` + positions indexadas como opciones; sim:
  sus actionTypes.
- `fm/web/static/index.html`: Vue 3 (`vue.global.prod.js` 3.5.13 vendorizada,
  sin internet ni build). Tarjeta por robot con estado derivado del `state`
  (sin conexión / manual / E-stop / error / en pausa / cargando / ejecutando
  / en cola / libre), batería con umbrales, order + `actionStates`, texto
  `MISSION`, últimos 3 errores, botones habilitados según estado; panel de
  flota con catálogo unificado (unión de actions de todos los robots,
  parámetros con desplegable si hay opciones) y las últimas 5
  `order_response`; eventos (cambios de actionStatus, respuestas, errores
  nuevos, connection); reconexión del WS con backoff y banner.
- `tests/test_web.py` (TestClient: catálogo, POSTs publican en el bus,
  WS entrega el último estado).

### Decisiones nuevas

44. **La UI es un cliente VDA más** (plan §1): sin API paralela de estado;
    lo que ve el navegador es el espejo de MQTT y lo que envía pasa por el
    broker. Coste: la UI depende de que el broker esté; ventaja: cero
    lógica duplicada y demostración viva del contrato.
45. **El hilo web no lee el estado vivo de `Robot`**: solo configuración
    (`fleet_info`) y el bus. Así no hay carrera con el hilo principal ni
    con los hilos de `poll()`.
46. **Vue vendorizada** en `fm/web/static/` en vez de CDN: el taller puede
    no tener internet y la página debe abrir igual.
47. **Sin autenticación** (decisión del usuario, LAN de clase); si sale de
    ahí, un token en los `POST` es el mínimo.

## 2026-09-18 — Positions "duplicadas": entry positions de marcadores

Aclarado con `GET /positions/<guid>` en mir-2 y con el usuario. No son
duplicados: cada VL-marker (`type_id` 11) y cada cargador (20) lleva una
**entry position** automática (12 / 21) con el mismo nombre y `parent_id`
apuntando al marcador, ~1.3 m delante (p.ej. `H2D1-VL`: marcador en
(28.68, 4.82), entry en (30.00, 4.83)). En la web del MiR solo se elige el
marcador y el robot pasa por su entry él solo; la entry no es un destino.

### Decisiones nuevas

48. **El índice de positions ignora las entry positions** (`type_id` 12 y
    21, `POSITION_ENTRY_TYPES` en `client.py`): `target_pos=H2D1-VL` →
    GUID del marcador (11), igual que al elegirlo en la web. Es lo que ya
    pasaba de facto ("se usa la primera"), pero ahora es explícito, sin
    warnings de "position duplicada" al arrancar, y el desplegable de la UI
    lista cada marcador una sola vez. Los warnings de duplicado quedan para
    duplicados reales (mismo nombre en varios mapas, decisión 15).

## 2026-09-18 — `factsheet` (§6.11)

`pytest`: 76 en verde. Probado con el sim: retained al arrancar
(`headerId 1`) y `factsheetRequest` → `FINISHED` + republicación (`headerId 2`).

### Código

- `fm/adapters/base.py`: `RobotSpec` y gancho opcional `describe_robot()`.
  MiR: `MIR250_SPEC` (800×580×300 mm, 250 kg, 2.0 m/s, DIFF, CARRIER,
  NATURAL/AUTONOMOUS). Sim: ficha genérica "SIM".
- `fm/vda5050/factsheet.py`: `build_factsheet(header, actions, spec)` y
  `factsheet_for(header, driver, ...)`. `mobileRobotActions` = actions del
  driver (scope NODE, `actionParameters` STRING, `pauseAllowed`/
  `cancelAllowed` true) + instantActions del core (scope INSTANT).
  `protocolLimits`: `order.nodes` 1, `order.edges` 0, `node.actions` 1,
  intervalos 0.5/1.0 s. `optionalParameters`: `actionParameters`,
  `blockingType`. `mobileRobotGeometry`/`loadSpecification` vacíos.
- `run_fm.py`: `publish_factsheet(r)` retained tras conectar;
  `Dispatcher` lo recibe como callback y `factsheetRequest` lo llama
  (antes → FAILED).
- `schemas/factsheet.schema.json`: parche `pauseAllowed`/`cancelAllowed`
  `string` → `boolean` (el PDF §7.10 p. 97 dice boolean).
- Tests: `tests/test_factsheet.py` (MiR, sim, driver sin ganchos, sin
  actions: todos validan contra el schema).

### Decisiones nuevas

49. **El factsheet lo construye el core**, como el `state`: el driver solo
    aporta ficha y actions. Un driver sin ganchos obtiene un factsheet
    válido con el nombre del driver como `seriesName` y las actions de
    `fleet.yaml` sin parámetros.
50. **`valueDataType` de todos los parámetros es `STRING`**: los
    `position_inputs` son nombres de position y los `required_inputs` se
    reenvían tal cual; el tipo real lo decide la mission de Blockly y el FM
    no lo conoce. Si algún día hace falta, `ParamInfo.kind` puede crecer.

## 2026-10-07 — Parámetros numéricos de mission (`n_piezas_pedidas`)

Petición del usuario: poder mandar desde aguas arriba (y desde la UI) una
mission con parámetros. Primer caso: `n_piezas_pedidas`, un número que la
mission `MIRUR-prueba-parámetros` escribe en el **registro PLC 22**.

Comprobado con `GET /missions/<guid>/actions` en los dos robots (solo
lectura): la action `set_plc_register` tiene `register = "22"` fijo y su
`value` expuesto como `input_name: "n_piezas_pedidas"`, con valor por
defecto `1.0`. El parámetro tiene un `guid` distinto en cada robot
(`2e8a7b12…` en mir-1, `eeeabc4e…` en mir-2), igual que las missions, pero
`POST /mission_queue` lo identifica por `input_name` (gotcha §9.1, el mismo
mecanismo que `target_pos`), así que no hay que resolver nada por robot más
allá del GUID de la mission, que ya se hacía. Comprobado con `connect()` +
`translate()` contra los dos robots: body `{"id": "n_piezas_pedidas",
"value": 5}`, sin avisos. **Probado con robot real** (2026-10-07, el
usuario la lanzó desde la interfaz web): la mission se ejecuta y escribe el
valor en el registro 22.

`pytest`: 90 en verde.

### Código

- `fm/adapters/mir/config.py`: `ActionConfig.number_inputs`.
- `fm/adapters/mir/translate.py`: `to_number()` y bloque `number_inputs` en
  `from_vda_order`.
- `fm/adapters/mir/driver.py`: `ParamInfo(kind="number")` en
  `describe_actions`; `_check_inputs()` avisa al arrancar si `fleet.yaml`
  declara un input que la mission no expone en ese robot.
- `fm/vda5050/factsheet.py`: `valueDataType: NUMBER` para `kind == "number"`.
- `fm/web/static/index.html`: `<input type="number">` con `v-model.number`
  para los parámetros numéricos.
- `config/fleet.yaml`: action `prueba_parametros` en mir-1 y mir-2.
- Tests: normalización y rechazos en `tests/test_orders.py`, POST con número
  y aviso de input inexistente en `tests/adapters/test_mir_driver.py`,
  factsheet con `NUMBER`.
- Documentación: `README.md` (fila `number_inputs` en la tabla de config,
  `id` = `input_name` y no el `guid` del parámetro, ejemplo de order con
  número) y `CLAUDE.md` §8 (semántica de `number_inputs`).

### Decisiones nuevas

51. **Tipo numérico explícito en `fleet.yaml` (`number_inputs`)** en vez de
    deducirlo del valor por defecto de la mission (`1.0`): se ve en la
    config qué espera cada action y el FM no depende de un detalle de
    Blockly. VDA no fija el tipo de `actionParameters.value` y cada emisor
    manda lo suyo (la UI y los scripts, texto; otro emisor, número), así que
    el FM normaliza a int si es entero (los registros PLC 1–100 del MiR son
    enteros) o a float; acepta coma decimal; rechaza bool, NaN e infinito.
52. **Matiza la decisión 50**: los parámetros `number` se anuncian en el
    factsheet como `valueDataType: NUMBER`; el resto sigue en `STRING`.
53. **Los inputs se validan contra la mission al arrancar solo con un
    warning** (no error): coherente con missions inexistentes (§8), el FM
    arranca igual y el fallo saldría como 400 del MiR en esa order.

## 2026-10-07 — Allowlist de positions por action y rango numérico (deslizador)

Mission nueva `MIRUR-prueba-parámetros-v2` con los dos parámetros de la
mission final: `target_pos` (paso *move*: **mueve el robot**, y luego lanza
una submission) y `n_piezas_pedidas` (registro PLC 22). Comprobado con GETs
en los dos robots: existe en `mirur-tknika` y las positions `Deshecho`,
`Montaje` y `Area_calidad` existen en ambos (GUIDs distintos, como siempre).

Peticiones del usuario: limitar `target_pos` a esas tres positions en la
interfaz, y un deslizador para `n_piezas_pedidas` (uso desde tablet) que
conviva con el recuadro numérico, sincronizados.

Comprobado: `connect()` + `describe_actions()` + `translate()` contra los
dos robots (sin postear): opciones = las tres positions, rango 1–10,
`Montaje`/`"7"` → GUID de cada robot / `7`; `H2D1-VL` → `NO_ROUTE_TO_TARGET`,
`12` → `VALIDATION_FAILURE`. UI comprobada en jsdom con la página real:
opciones, valor inicial = mínimo, deslizador ↔ recuadro en los dos sentidos
y POST con número. `pytest`: 93 en verde. Lanzada por el usuario desde la
web en mir-2 (cola 381, 2026-10-07 10:15 UTC): el MiR recibió `target_pos`
= GUID de `Montaje` (label "Montaje") y `n_piezas_pedidas = 4.0`; la paró
él a mano (ver entrada siguiente). **Pendiente:** verla en una tablet.

### Código

- `fm/adapters/base.py`: `ParamInfo.min/max/step`.
- `fm/adapters/mir/config.py`: `NumberRange`; `ActionConfig.number_ranges`
  y `ActionConfig.positions_allowlist`; `number_inputs` acepta lista o
  mapping con rango (`_parse_numbers`, valida min ≤ max y step > 0).
- `fm/adapters/mir/translate.py`: allowlist de la action
  (`NO_ROUTE_TO_TARGET`) y rango (`VALIDATION_FAILURE`).
- `fm/adapters/mir/driver.py`: `_position_choices()` (positions del robot ∩
  allowlist del robot ∩ allowlist de la action; sin índices, la de la
  action); rango en `describe_actions`; warning al arrancar si una position
  de la allowlist no existe en el robot.
- `fm/vda5050/factsheet.py`: la descripción del parámetro lista las
  positions admitidas (≤ 10) o el rango (`number en [1, 10]`).
- `fm/web/static/index.html`: `type=range` + `type=number` con el mismo
  `v-model.number`; al elegir action, los números con rango arrancan en
  `min`; el recuadro lleva `min`/`max` (el navegador no deja enviar fuera).
- `config/fleet.yaml`: action `prueba_parametros_v2` en mir-1 y mir-2.
- Tests en `test_config.py`, `test_orders.py`, `adapters/test_mir_driver.py`,
  `test_factsheet.py`.

### Decisiones nuevas

54. **La allowlist de la action es del FM, no solo de la UI**: el FM la
    valida en `from_vda_order`, así que una order del sistema aguas arriba a otra
    position también se rechaza (`NO_ROUTE_TO_TARGET`). Se suma a la del
    robot (hay que pasar las dos); la UI ofrece la intersección.
55. **El rango de un número se declara en `fleet.yaml`** y también lo valida
    el FM (`VALIDATION_FAILURE`), por el mismo motivo. El deslizador solo
    aparece con `min` y `max`; sin rango, solo recuadro. `step` no se
    valida en el FM (solo guía la UI). Rango 1–10 para `n_piezas_pedidas`
    elegido por Claude como valor razonable; **confirmado por el usuario** el 2026-10-07.
56. **Un solo `v-model` para deslizador y recuadro**: la sincronización la
    hace Vue sin código propio. Al elegir la action el valor arranca en
    `min` para que los dos muestren lo mismo y no se envíe vacío.

## 2026-10-07 — Motivo del FAILED (`actionResult`)

El usuario vio en la UI la order de la cola 381 (mir-2) como `FAILED` sin
saber si se había abortado a mano o había fallado. `GET mission_queue/381`:
`state: "Aborted"`, `message: "Aborted - User Request"`. Otra entrada (381
de mir-1, de enero) trae `message: "Aborted on startup"`. El MiR termina
en `Aborted` tanto si se para a mano como si la mission falla; lo único que
lo distingue es `message`, que el FM descartaba.

VDA 5050 solo tiene `actionStatus: FAILED` (no hay ABORTED), pero
`actionStates[].actionResult` es un texto libre para el resultado.

`pytest`: 97 en verde. UI comprobada en jsdom (tarjeta y evento).

Confirmado con fallos reales (el usuario lo vio en la UI; textos leídos
con `GET mission_queue/<id>` el mismo día):

| `state` | `message` | Caso |
|---|---|---|
| `Aborted` | `Aborted - User Request` | parada a mano (mir-2 381, mir-1 1387) |
| `Aborted` | `Unable to find path to goal.` | sin camino al destino (mir-1 1385, 1388) |
| `Aborted` | `Charging failed to start..` | la carga no arrancó en el dock (mir-2 377) |
| `Aborted` | `Aborted on startup` | entrada viva al reiniciar el robot (mir-1 381) |
| `Done` | `ActionList was executed without problems..` | fin normal (sale como `actionResult` del FINISHED) |

Es decir: **todo lo que no acaba bien es `Aborted`**, y solo `message`
dice si fue una parada o un fallo.

### Código

- `fm/adapters/base.py`: gancho opcional `job_result(job_id)` en el
  `Protocol` y helper `job_result(driver, job_id)` (None si el driver no lo
  implementa).
- `fm/adapters/mir/driver.py`: `job_status` guarda `message` al terminar;
  `job_result` lo entrega (una vez).
- `fm/adapters/sim/driver.py`: "fallo simulado (fail_actions)" / "cancelado".
- `fm/orders.py`: `ActiveOrder.result` → `actionResult`; el error
  `ORDER_EXECUTION_FAILED` lleva el motivo (`… terminó FAILED: Aborted -
  User Request`) y se loguea. `cancelOrder` → "cancelada por cancelOrder".
- `fm/charge.py`: el motivo en el log de una carga FAILED.
- `fm/web/static/index.html`: motivo junto al estado en la tarjeta y en
  el evento.

### Decisiones nuevas

57. **El motivo viaja en `actionResult`, no como estado nuevo**: no se
    inventa un `ABORTED` fuera del enum VDA; cualquier cliente VDA lo lee.
    El FM no interpreta el texto (no decide "parada a mano" vs "fallo"):
    lo pasa tal cual porque los textos del MiR no están documentados y
    cambian entre versiones. Se aplica también a `FINISHED` si el MiR
    trae `message`.

## 2026-10-07 — Columna "resultado" en la tabla de eventos

Petición del usuario: en vez del motivo entre paréntesis al final de la
línea (`RUNNING → FAILED (Aborted - User Request)`), una columna propia en
los eventos para reconocerlo de un vistazo, aunque solo se rellene en los
fines de mission. Probado por el usuario en la web: se queda.

- `fm/web/static/index.html`: eventos con cabecera (hora · quién · evento ·
  resultado); `addEvent(who, text, iso, result, status)`; la columna lleva
  `actionResult` coloreado por `actionStatus` (FAILED en rojo y negrita,
  FINISHED en verde). La tarjeta del robot no cambia (motivo junto al
  estado).
- Comprobado en jsdom: parada a mano, "Unable to find path to goal." y fin
  normal, cada uno en su fila; las filas sin resultado quedan vacías.

## 2026-10-09 — Llave física manual/auto (`mode_key_state`)

Con la llave de mir-2 en manual, `run_fm` seguía mostrando
`mode=AUTOMATIC`. Causa: el FM solo ponía `MANUAL` con `state_id = 11`, que
es *ManualControl* (joystick de la web tras pulsar el botón azul de aceptar);
girar la llave no cambia `state_id` (se quedó en 10 EmergencyStop). La
posición de la llave va en `/status.mode_key_state`; `mode_id`/`mode_text`
(7 Mission) distinguen Mission/Mapping, no manual/auto.

Valores de `mode_key_state` vistos con mir-2 real: `"auto"` (automático),
`"manual"` (manual) e `"idle"` (posición central/neutra; el robot queda en
`state_id = 10` EmergencyStop).

### Código

- `client.py`: `MirStatus.mode_key_state` + constantes `KEY_AUTO`/`KEY_MANUAL`/`KEY_IDLE`.
- `translate.py`: `operating_mode = MANUAL` si `state_id = 11` **o** llave
  en `manual`; `available = False` si la llave no está en `auto`, con
  motivo "llave del robot en '…'". `key=` añadido al `information` DEBUG.
- `scripts/read_status.py` muestra la llave; test `test_llave_fisica`.

### Decisiones nuevas

58. **Cualquier llave distinta de `auto` bloquea orders** (`idle` incluido,
    y también un valor que no conozcamos de otro firmware); solo
    `manual` cambia `operatingMode` a `MANUAL`. Si `mode_key_state` no viene
    (firmware antiguo) no bloquea, para no dejar inutilizado un robot que no
    informa.

### `operatingMode` con la llave en neutra

Leída la norma (§6.6.6, tablas 10 y 11): `INTERVENED` = la flota no tiene el
control, el robot informa bien y **no borra la order**; si vuelve a
`AUTOMATIC` la continúa. Es lo que hace el MiR en neutra (EmergencyStop con
la mission en cola), mientras que `AUTOMATIC` ("la flota tiene el control
total") era falso.

59. **Llave en neutra → `INTERVENED`, rechazando orders igualmente.** La
    norma permite enviar orders en `INTERVENED` (el robot las guarda), pero
    no queremos que el asignador dé trabajo a un robot que alguien está
    manipulando, ni dejar una order dirigida esperando a que se gire la
    llave. Desviación documentada en el README (limitaciones). Ídem con
    `MANUAL`: la norma pide borrar la order al entrar y el MiR la conserva.
    La UI muestra "intervenido".

## 2026-10-11 — Diseño: configurador del FM y sincronización de la flota

Sin código. Plan completo en `docs/plan-admin.md`; se refinará tras las
pruebas de GUIDs con robot (previstas el 2026-10-13).

### Decisiones nuevas (provisionales hasta la fase 0)

60. **Dos pestañas nuevas separadas de la de operación:** configurador del
    FM (edita `fleet.yaml`) y sincronización (missions, positions y mapas
    en los robots). Un operario básico no las necesita.
61. **Sin editor de missions:** la lógica se programa en el Blockly de un
    MiR; el FM la captura y la propaga.
62. **Sincronización puntual y pensada para N robots:** se usa con una
    mission o position nueva o con un MiR nuevo; no es continua.
63. **Catálogo versionado en git (`catalog/`) entre el robot y la flota**
    (capturar → desplegar), en vez de copiar robot a robot: no depende de
    que el robot origen esté disponible, deja historial y permite sacar el
    siguiente GUID sin tener toda la flota encendida.
64. **Estándar de GUID `4d495255-TTTT-0000-0000-NNNNNNNNNNNN`** ("MIRU" +
    tipo + contador), solo hexadecimal por si el MiR valida formato UUID.
    El usuario no lo edita. Pendiente de validar con robot.
65. **`fleet.yaml` pasa a un catálogo único de actions** y cada robot lista
    las que tiene habilitadas: hoy cada action se repite por robot, cosa
    inmanejable con N robots y en una UI.

## 2026-10-12 — Interfaz web con el estilo de la web del MiR

Solo CSS de `fm/web/static/index.html`; plantilla y lógica sin cambios.
Referencias en `img_referencia/` (capturas de la web del MiR250).

### Decisiones nuevas

66. **La UI imita el estilo general de la web del MiR, no su estructura:**
    fondo gris claro, tarjetas blancas planas con esquinas redondeadas
    (10 px; 6 px en botones, campos y etiquetas),
    títulos y botón principal en azul marino, verde MiR para "libre" y
    etiquetas de estado en mayúsculas con borde y fondo pálido (como
    EXECUTING / PENDING de su cola), botón de cancelar en rojo relleno.
    Motivo: el operario reconoce los mismos códigos de color que ya ve en
    el robot. No se copia la barra superior del robot (batería, cola):
    aquí hay N robots y cada tarjeta ya lo muestra. Se mantiene el modo
    oscuro con los mismos tokens.
67. **Pestañas en una barra lateral de iconos, como la del MiR:**
    Operación (la interfaz de siempre) arriba; Configurador y
    Sincronización en un grupo aparte debajo, porque no son para el
    operario (decisión 60). El nombre sale en un rótulo al pasar el ratón
    o al llegar con el tabulador. La pestaña va en el `#` de la URL
    (`#operacion`, `#configurador`, `#sincronizacion`): se puede enlazar
    de una a otra (el configurador enlazará a la sincronización) y al
    recargar no se pierde. Todo en el cliente, sin cambios en el
    servidor. Las dos pestañas nuevas son de momento un esqueleto con las
    secciones de `docs/plan-admin.md` §2.2 y §3.4 marcadas "en diseño";
    se rellenan tras la fase 0. Recordatorio (§7 del plan): una pestaña
    aparte no protege nada.

## 2026-10-12 — Revisión del código: un fallo corregido y limpieza

`pytest`: 104 en verde (6 nuevos). Probado con `fleet-sim.yaml` (asignación,
repetida ignorada, ocupado → otro robot, `cancelOrder`, auto-carga, `state`
contra el schema, `OFFLINE` al cerrar) y con `fleet.yaml` y los dos MiR
apagados (backoff, `REJECTED NO_MOBILE_ROBOT_AVAILABLE`). Sin hardware.

### Código

- **Fallo corregido:** si el robot no aceptaba el job al encolarlo (red caída
  justo entonces, o un 4xx del MiR al hacer `POST /mission_queue`), la
  excepción subía hasta `Dispatcher.drain()`, que solo la registraba en el
  log: una `fleet/order` se quedaba **sin `order_response`** y una
  `<serial>/order` sin error en `state.errors`. Ahora `Robot.execute()` la
  convierte en `OrderRejected` y sigue el camino de cualquier rechazo.
- `Robot.state(header)`: construye el `state` en un solo sitio; `run_fm.py`
  lo usaba duplicado en `publish_state` y `tick_robot`.
- Gancho `extra_state()` del contrato de drivers: estaba declarado en
  `RobotDriver` y en el README pero el core no lo llamaba nunca. Ahora lo
  llama `Robot.state()` (los dos drivers actuales no hacen nada en él).
- `connect()` fallido con el robot respondiendo se reintenta cada
  `CONNECT_RETRY_S` (5 s) en vez de en cada tick.
- Duplicados unificados: `JOB_ALIVE` (en `fm/adapters/base.py`), `drop_none`
  (antes `_clean`, copiado en `state.py` y `factsheet.py`) y
  `describe_actions(driver, action_types)` (lo calculaban igual el factsheet
  y la web). Quitado `MqttBus.publish_state`, que no se usaba.
- Al cerrar limpio el log ya no dice "paho reintentará".

### Decisiones nuevas

68. **Un job que el robot no acepta es un rechazo de la order**, con
    `ROBOT_UNREACHABLE` si no se llegó al robot (`OSError`: de ahí heredan
    las excepciones de red de `requests`) y `ORDER_EXECUTION_FAILED` si el
    robot respondió que no. Motivo: el emisor de `fleet/order` necesita
    siempre una respuesta para poder reencolar. Si fue un timeout de lectura
    y el MiR llegó a encolarla, el FM la verá como trabajo ajeno
    (`foreign_busy`) y no mandará nada más a ese robot mientras dure.
69. **`extra_state()` solo se llama con telemetría fresca**, y si lanza
    excepción se registra y se publica el `state` igual: un driver con
    un fallo no debe parar el tick de toda la flota.
70. **`connect()` se reintenta cada 5 s, no en cada tick:** con un
    `mission_group` mal escrito eran varias peticiones REST y un warning
    por segundo. El primer intento tras volver a responder sigue siendo
    inmediato; solo si falla, un robot recién encendido tarda hasta 5 s
    más en aceptar orders.

## 2026-10-13 — Fase 0 de `plan-admin`: GUIDs elegidos y lectura de mapas

Sin código en el FM; scripts de prueba desechables. Lecturas (GET) en los
dos MiR; escrituras solo en mir-2 (`192.168.15.15`). Sin missions lanzadas.

### Lo que se ha comprobado

- **`POST /sessions` admite un GUID elegido**, que tiene que tener
  **exactamente 36 caracteres** de cualquier tipo. Aceptó
  `estoesunapruebadequepuedoponeresto01` (sin guiones ni hexadecimal). Con
  35 o 37 caracteres responde `400 input_string_too_short` /
  `input_string_too_long`. Repetir un GUID que ya existe da `409
  duplicate_entry` y deja intacta la session existente. `created_by_id` es
  opcional (pone el usuario del token). Crear una session no la activa
  (`active: false`). La session de prueba se borró al terminar.
- **GUIDs de fábrica del MiR**, iguales en los dos robots y no
  hexadecimales: `mirconst-guid-0000-0001-areas0000000` (ConfigurationSite),
  `…-0001-maps00000000` (ConfigurationMap) y `…-0004-users0000000`
  (Distributor).
- **Las sessions también tienen GUIDs distintos en cada robot:** `Suberri`
  en mir-1 y `suberri` en mir-2. Como el mapa pertenece a una session,
  también hay que unificarlas, y van las primeras en el orden de despliegue.
- **`GET /maps/<guid>`:** la imagen va en `base_map`, que es un PNG en gris
  de 8 bits codificado en base64, sin prefijo `data:`. Píxeles: `0` =
  obstáculo, `255` = libre, `200` = sin explorar. La fila 0 de la imagen
  corresponde a la y máxima (las 49 positions de los dos robots caen sobre
  píxeles libres solo invirtiendo el eje y). `origin_x/y` es la esquina
  inferior izquierda en metros y `resolution`, metros por píxel. El mapa
  `suberri` mide 1053×631 px a 0,05 m, es decir, 52,65 × 31,55 m. No hay
  una capa aparte para las paredes dibujadas: parecen quedar dentro de
  `base_map`.
- **Lo que no va en `POST /maps`** y se crea aparte: positions
  (`/maps/<g>/positions`), zonas (`/zones`, que devuelve los mismos objetos
  que `/area_events`; las 5 tienen la misma geometría en los dos robots) y
  path_guides (ninguna de momento).
- **Los dos mapas `suberri` ya no son iguales:** salieron del mismo PNG
  (`img_referencia/mapa_base/MiR floor plan.png`), pero difieren en 2.008
  píxeles (paredes dibujadas en el taller) y en las positions (26 en mir-1,
  23 en mir-2). El PNG difiere en 165 píxeles del mapa de mir-2 y en 1.843
  del de mir-1.
- **`POST /maps` admite un GUID elegido.** Se creó `mapa_prueba` en mir-2,
  dentro de `suberri_mirur`, con GUID
  `imperialfleet-map0000000-01-0001-001`, ese PNG tal cual (mismo formato
  que guarda el MiR) y la resolución y el origen de `suberri`. Al leerlo de
  vuelta, el GUID se conserva y `base_map` es idéntico byte a byte: el MiR
  no convierte la imagen. Crear el mapa no lo activa; el robot sigue en
  `suberri`.

### Decisiones nuevas

71. **GUID legible `imperialfleet-<tipo>-<inst>-<padre>-<número>`**
    (formato completo en `docs/plan-admin.md` §4). Sustituye a la
    decisión 64. Ejemplo: `imperialfleet-position00-01-0001-012` es la
    position 12 del mapa 1 de la instalación 01. Motivo: el MiR acepta
    cualquier cadena de 36 caracteres, y así quien haga una petición sabe
    qué recurso es con solo ver el GUID. El tipo va en inglés, como el
    endpoint, y solo se usan minúsculas, cifras y guiones porque el GUID
    viaja en las URL.
72. **Contador por padre solo en los recursos sin hijos** (position, zona,
    path_guide, action). Los recursos que tienen hijos (mapa, mission) se
    numeran en toda la instalación y su campo padre es solo informativo.
    Motivo: si los mapas se numerasen dentro de su session, el mapa 1 de
    dos sessions distintas daría GUIDs repetidos a sus positions. El
    siguiente número se calcula a partir de los GUIDs existentes, no del
    padre actual.
73. **Las sessions (sites) también se unifican.** El orden de despliegue
    queda así: session → mapa → positions y zonas → missions. Primera
    session: `suberri_mirur` = `imperialfleet-session000-01-0000-001`,
    creada en mir-2.
74. **El mapa `suberri` y su session no se tocan nunca.** Las pruebas se
    hacen en mir-2, en la session `suberri_mirur` y en su mapa. El mapa se
    creó como `mapa_prueba` y el usuario lo renombró a `suberri_mirur`,
    con el mismo GUID.
75. **Catálogo en `catalog/`, un JSON por recurso con su GUID como nombre**
    (estructura en `catalog/README.md`). La imagen del mapa va en un `.png`
    aparte, para poder abrirla y para no meter en git un base64 de 11.000
    caracteres. Las referencias van ya traducidas a GUIDs del catálogo, y
    `_origen` guarda el robot y el GUID de procedencia, que es lo que
    hace falta para adoptar lo existente. Primera captura, con
    `scripts/catalog_capture.py`: las 23 positions de `suberri` de mir-2,
    colgando del mapa `imperialfleet-map0000000-01-0001-001`. Se eligió
    mir-2 porque las pruebas se hacen en él. mir-1 tiene además
    `Pos_clase_2`, `MESA_TKNIKA` y `TOOLCHANGER`, y no tiene
    `Espera_cercana`.
76. **Tipo `dockoffset` para los docking offsets de los marcadores**, con
    el mismo padre y número que su position. Las positions hijas (entrada
    de marcador o cargador, tipos 12/21) llevan UUID versión 4 en el MiR:
    probablemente las crea él solo y no podremos elegir su GUID. Por
    probar al crear las positions.

### Creación de positions en mir-2 (mapa `suberri_mirur`)

- **`POST /positions` admite un GUID elegido.** `H2D1` (tipo 0) se creó
  con `imperialfleet-position00-01-0001-001` y sus coordenadas exactas.
- **Un marcador (tipo 11) exige `offset_x`, `offset_y` y
  `offset_orientation`** (sin ellos, 400). Son los valores del docking
  offset: con los capturados (−1.086, 0.014, 1.04), el MiR crea solo un
  docking offset con esos valores y **GUID propio** (UUID versión 1).
- **La position de entrada (tipo 12) también la crea el MiR solo**, con
  GUID propio (UUID versión 4) y `parent_id` apuntando al marcador. La
  coloca 1 m más allá del punto de atraque (x 30.76); en `suberri` estaba
  movida a mano a x 30.0. Se dejó en 30.0 con `PUT /positions/<guid>`
  (el PUT funciona).
- Consecuencia: en un marcador solo podemos elegir el GUID del propio
  marcador. La entrada y el docking offset quedan con GUIDs distintos en
  cada robot, y el despliegue tendrá que encontrarlos por su `parent_id` /
  `pos_id` y corregir su posición o sus valores con PUT. Falta probar si
  se puede borrar la entrada automática y crearla con nuestro GUID.
- Un cargador (tipo 20) se crea sin `offset_*` y el MiR coloca su entrada
  (tipo 21) en el sitio correcto. En las entradas de los marcadores hubo
  que corregir la x con PUT, igual que en `H2D1-VL`.
- **Las 23 positions de `suberri` están creadas en mir-2**, en el mapa
  `suberri_mirur`, con `scripts/catalog_deploy.py`. Una segunda pasada da
  0 cambios, y los docking offsets coinciden con el catálogo.

77. **No se unifican los GUIDs de lo que el MiR crea solo** (entrada de
    marcador o cargador y docking offset). Sustituye a la parte de GUIDs
    de la decisión 76. Motivo: ninguna de las 62 missions de mir-2 los usa
    (12 acciones usan el marcador o el cargador, que sí lleva nuestro
    GUID). Borrarlos y crearlos de nuevo con nuestro GUID añadiría pasos y
    el riesgo de dejar mal el atraque, sin ganar nada. El despliegue crea
    la madre (con los `offset_*` si es un marcador), busca la entrada por
    `parent_id` y la corrige con PUT si no está donde dice el catálogo.

### Missions al catálogo

Lectura de las missions de `mirur-tknika` en mir-2:

- Cada mission tiene `group_id` y **`session_id`**. Queda por ver si una
  mission de `suberri_mirur` se ve con el site activo `suberri`.
- Las actions se ordenan por `priority`. El anidamiento no apunta a la
  action padre sino al **GUID de un parámetro** suyo (`scope_reference` →
  parámetro `try`/`catch` de un `try_catch`, o los de un `if`).
- Qué referencian por GUID: positions (`move`, `docking`), missions
  (`load_mission`), un footprint y sonidos. El footprint y dos de los
  sonidos son de fábrica (`mirconst-…`, iguales en los dos robots). Hay un
  sonido subido a mano (`piezaH2D1`, solo en mir-2), con el audio en base64
  en el campo `sound` (OGG).
- `Carga en estación MIRUR` y `Simulacion Recogida pieza H2DX MIRUR` llaman
  a `Ir a zona de espera`, del grupo `p6-factory` (proyecto anterior).
- `GET /missions/<g>/definition` no es una exportación: describe la
  mission como bloque para usarla dentro de otras.

78. **De una mission solo se unifica el GUID de la mission y el de su
    grupo**, que son lo que referencian `load_mission` y el FM. Las actions
    y sus parámetros quedan con el GUID que les ponga el MiR, como en la
    decisión 77. `scope_reference` se guarda como (priority de la
    contenedora, id del parámetro) y se rehace al desplegar. El tipo
    `action0000` se quita del §4 y entra `sound00000` para los sonidos
    subidos a mano.
79. **Grupo nuevo `mirur-tknikav2`** (`imperialfleet-missiongrp-01-0000-001`),
    para no tener dos grupos con el mismo nombre mientras conviva con
    `mirur-tknika`. Cuando todo funcione se renombrará a `mirur-tknika`
    (el GUID no cambia). **Al catálogo va todo lo que llama
    `mirur-tknika`**: sus 9 missions, `Ir a zona de espera` de
    `p6-factory` (en el mismo grupo del catálogo; `_origen.grupo` recuerda
    de dónde vino) y el sonido `piezaH2D1`. Capturado con
    `catalog_capture.py missions`; para capturar positions el script pasa
    a ser `catalog_capture.py positions …`.

### Primera mission desplegada en mir-2

- `POST /mission_groups` y `POST /missions` **admiten GUID elegido**.
  Creados `mirur-tknikav2` (`…-missiongrp-01-0000-001`, con priority 0;
  el original tiene 102) y `Ir a zona de espera MIRUR`
  (`…-mission000-01-0001-004`), en la session `suberri_mirur`.
- Las actions se crean una a una con `POST /missions/<g>/actions`, en
  orden de `priority`. Tras crear la contenedora (`try_catch`) se leen los
  GUIDs que el MiR ha puesto a sus parámetros, y con ellos el
  `scope_reference` de las actions de dentro. Al releer la mission y
  normalizarla igual que en la captura, sale **idéntica al catálogo**, y
  el MiR la da por `valid`.
- El usuario lo ha comprobado en la web: con el site activo `suberri` se
  ven el grupo y la mission, y Blockly la pinta igual que la original.
  **Las missions no dependen del site activo.**

### Grupo `mirur-tknikav2` completo en mir-2

- `scripts/catalog_deploy.py missions` despliega lo que necesita un grupo:
  primero los sonidos y el grupo, después las missions en orden de
  dependencias (`Ir a zona de espera` antes de `Carga en estación MIRUR`).
  No crea una mission si le falta en el robot alguna position o
  sub-mission que use. Las positions pasan a desplegarse con
  `catalog_deploy.py positions …`.
- Desplegadas en mir-2 las 10 missions del catálogo. Al releerlas todas
  coinciden con el catálogo, una segunda pasada da 0 cambios y el MiR las
  da todas por `valid`.
- `POST /sounds` **admite GUID elegido**, pero **vuelve a codificar el
  audio** (OGG de 23.897 → 22.123 bytes, misma duración). Los sonidos se
  comparan por GUID, nombre y duración, no byte a byte.
- El usuario ha lanzado `MIRUR-prueba-parámetros` (copia en
  `mirur-tknikav2`) en mir-2: funciona bien.

## 2026-10-13 — Pestaña Sincronización (primera versión)

`fm/adapters/mir/sync.py` (lógica), tres rutas en `fm/web/server.py` y la
pestaña en `index.html`. `pytest`: 113 en verde (9 nuevos). Probado contra
los dos robots solo con vistas previas: mir-2, todo el catálogo "igual";
mir-1, todo por crear.

80. **Se sincroniza una selección, no todo el catálogo.** Lo habitual será
    llevar una mission o unas positions. La selección se completa sola con
    sus dependencias (`Catalog.closure`): una mission arrastra su grupo, su
    session, sus positions, sus sub-missions y sus sonidos; una position,
    su mapa; un marcador, su entrada. Como lo que el robot ya tiene igual
    no se toca, sincronizar una mission suelta solo escribe esa mission.
    "Todo" es seleccionarlo todo.
81. **La lógica sale de los scripts a `fm/adapters/mir/sync.py`**, en
    cuatro pasos: cierre de la selección, lectura del robot (solo GET),
    planificación **pura** (una operación por recurso: crear, corregir,
    igual o distinta) y ejecución. Si una operación falla, las que dependen
    de ella se saltan. "Distinta" no se toca: sustituir queda para más
    adelante, con su propia vista previa. `scripts/catalog_deploy.py` es
    ahora un envoltorio (`<serial> <guid>… | --todo [--escribir]`). Tests
    con un MiR falso en memoria que imita lo visto con robot (entrada de
    marcador automática, GUIDs propios de actions y parámetros).
82. **La sincronización es una capacidad opcional del driver**
    (`driver.make_sync(catalog_dir)`, solo en `MirDriver`), no parte del
    `RobotDriver`. La web la busca con `hasattr`, y el core sigue sin saber
    de MiR; un robot `sim` sale como "no aplica". Cada robot usa un
    cliente REST propio para sincronizar, distinto del del poll.
83. **Vista previa obligatoria antes de desplegar.** El botón Desplegar
    solo se activa si la vista previa corresponde a la selección y al robot
    actuales. El servidor vuelve a planificar al desplegar, porque el robot
    puede haber cambiado. Se rechaza (409) si el robot no responde, está
    ocupado (order, carga o mission ajena) o ya tiene otro despliegue en
    curso. El despliegue corre en un hilo por robot y su avance llega por
    el WebSocket con el topic `fm/sync/<serial>`.
84. **La captura sigue siendo por script** (`catalog_capture.py`). El botón
    "capturar desde robot" queda para una versión posterior.

El usuario desplegó el catálogo en **mir-1** desde la pestaña: todo
correcto. mir-1 tiene ya la session y el mapa `suberri_mirur`, las
positions, el grupo `mirur-tknikav2` y sus missions con los mismos GUIDs
que mir-2.

## 2026-10-13 — Retirado el grupo antiguo; `mirur-tknikav2` pasa a `mirur-tknika`

85. **El grupo `mirur-tknika` original se ha borrado en los dos robots y
    `mirur-tknikav2` (`imperialfleet-missiongrp-01-0000-001`) se llama ahora
    `mirur-tknika`**, también en el catálogo. Como el nombre y los nombres
    de las missions coinciden, `fleet.yaml` no cambia: al arrancar, el FM
    indexa 11 missions en mir-1 y 10 en mir-2, sin avisos. Antes de borrar
    se comprobó que ninguna mission de otro grupo llamaba a las del grupo
    antiguo y que ninguna estaba en cola, y se guardó una copia (JSON
    crudo de cada mission con sus actions) fuera del repo. Se paró el FM
    durante la operación, porque tenía indexados los GUIDs antiguos.
    - mir-1 tenía en el grupo antiguo una mission que no estaba en el
      catálogo, `traer`, que atraca en el marcador `MESA_TKNIKA` (solo en
      mir-1). Por decisión del usuario se capturó antes de borrar:
      `MESA_TKNIKA` → `…-position00-01-0001-024` (entrada, `-025`), con su
      docking offset calibrado con mir-1, y `traer` →
      `…-mission000-01-0001-011`, con su sub-mission apuntando a la
      `Ir a zona de espera MIRUR` del catálogo. Desplegada solo en mir-1.
    - Para renombrar un grupo basta con `PUT /mission_groups/<guid>
      {"name": …}`; el GUID no cambia.

## 2026-10-13 — Saber qué no está sincronizado

`pytest`: 115 en verde. Probado con los dos robots: la comprobación rápida
da los dos al día con lo que piden sus `fleet.yaml` (mir-2 no tiene `traer`
ni `MESA_TKNIKA`, que no usa); la completa en mir-2 da 37 iguales, 3 que
faltan y ninguno distinto.

86. **Qué tiene cada robot se comprueba en dos niveles.** La **rápida**
    mira si existe cada GUID del catálogo: una lectura por tipo de
    recurso, detecta lo que falta y la hace sola `SyncMonitor` cada **5
    min** (elegido por el usuario), al arrancar y tras cada despliegue. La
    **completa** compara el contenido (es la vista previa de todo el
    catálogo), detecta además lo distinto y solo se lanza a mano ("A
    fondo"), porque son unas 100 lecturas por robot. El informe guarda su
    hora y la UI dice "hace X min". Corre en un hilo propio y se salta
    el turno si el robot está desplegando: cada `MirSync` tiene un `lock`
    para que comprobación, vista previa y despliegue no usen a la vez el
    mismo cliente REST.
87. **Las missions de `fleet.yaml` se nombran por su GUID del catálogo**
    (decisión del usuario; antes, por nombre). El driver indexa cada mission
    por nombre y por GUID, así que vale cualquiera de los dos: el nombre
    queda para missions que no están en el catálogo. Cada línea lleva el
    nombre en un comentario. Ese GUID es el que une el configurador con
    la sincronización.
88. **Lo que un robot "debe tener" son las missions de su `fleet.yaml`
    (actions y carga) y sus dependencias.** Es el mismo cálculo de
    dependencias que el de la sincronización (`MirSync.required`). La
    pestaña muestra por robot "al día", "faltan N" o "N distintas", qué
    falta y qué hay en `fleet.yaml` que no está en el catálogo. En el
    catálogo, al lado de cada recurso, en qué robots está (con más de 6
    robots, un contador). **Preparar** marca lo que falta y pide la vista
    previa. Cuando llegue el configurador, la misma información irá en
    su matriz action × robot, y el asignador podrá descartar el robot que
    no tiene grabada la mission.

## 2026-10-14 — Configurador: decisiones previas y siguiente candidato en el asignador

Repaso con el usuario de las cuatro preguntas abiertas del configurador
(`docs/plan-admin.md` §2) antes de programarlo, e implementado el punto 7
de "Pendiente". `pytest`: 121 en verde (6 nuevos). Sin probar con robot.

### Decisiones nuevas

89. **Si el robot elegido no puede traducir la `fleet/order`, se prueba el
    siguiente del ranking** (antes se rechazaba la order entera). El
    asignador devuelve el ranking completo (`AssignResult.ranking`) y sigue
    siendo puro; el `Dispatcher` lo recorre. Traducir no toca la red, así
    que probar varios robots no tiene efectos. Dos excepciones:
    `VALIDATION_FAILURE` corta (la order está mal y ningún robot la
    aceptaría), y un fallo de `execute` tampoco pasa al siguiente (tras un
    timeout el MiR pudo encolarla igual: dos robots con la misma order).
    Si fallan todos: el `errorType` común, o `NO_MOBILE_ROBOT_AVAILABLE`
    si son distintos, y el motivo de cada robot en la descripción. Con
    `prefer_not_charging`, el robot que carga va al final del ranking en
    vez de quedar fuera: "preferir" no es "descartar".
90. **El "robot sin la mission grabada" (punto 17) lo cubre la decisión
    89**, sin que el core consulte la sincronización (que es de MiR): un
    robot sin la mission ya falla al traducir porque su índice no tiene ese
    GUID. Una mission grabada pero **distinta** no descarta al robot: solo
    se detecta con la comprobación "A fondo", que se lanza a mano. Queda
    como aviso en la matriz del configurador.
91. **Aplicar la config: en caliente los cambios ligeros, reiniciando los
    estructurales.** En caliente: umbrales, `battery_min`, habilitar una
    action en un robot y los parámetros de las actions (son las casillas de
    la matriz, lo más frecuente). Reiniciando: alta o baja de robots y
    cambio de host o driver, porque un robot nuevo necesita su cliente
    MQTT con Last Will, su hilo de poll y su `connection` retained; la
    pestaña avisa "requiere reiniciar el FM". Un botón de reinicio
    (`os.execv`) queda para más adelante. Responde la pregunta 3 del plan.
92. **Nuevo formato de `fleet.yaml` sin compatibilidad con el antiguo y
    sin sobrescrituras por robot.** Solo hay dos ficheros: se migran en el
    mismo cambio y el formato antiguo da error al arrancar con la pista
    para migrar (como en la fase 3 de drivers). Cada robot lleva solo la
    lista de actions habilitadas: hoy no hay ningún caso real de action
    distinta por robot, y sin sobrescrituras cada casilla de la matriz es
    sí/no. Además, un dict en `robots.<serial>.actions` queda como señal
    inequívoca del formato antiguo.
93. **Edición del configurador por partes:** v1, casillas de la matriz,
    umbrales y `battery_min` (todo en caliente; el catálogo de actions y
    los robots, en solo lectura); v2, crear y editar actions del catálogo
    (el flujo principal: mission capturada → action habilitada → desplegar;
    la UI puede proponer qué inputs son de position y cuáles numéricos si
    el JSON capturado trae el tipo del parámetro, por comprobar); v3, altas
    y bajas de robots (piden reiniciar; mientras sean dos, a mano en el
    YAML).

## 2026-10-14 — Fase (a) del configurador: catálogo de actions en `fleet.yaml`

`pytest`: 129 en verde (8 nuevos). Arranque con `fleet-sim.yaml`
comprobado. Sin probar con los robots, pero comprobado que cada driver MiR
recibe exactamente el mismo bloque `actions` que con el formato anterior
(mir-1 y mir-2, comparando con el `fleet.yaml` de antes de migrar).

94. **El catálogo se resuelve en el core y el driver recibe el dict de
    siempre.** `load_config` lee `actions:` de la raíz y la lista de cada
    robot, y deja en `raw["actions"]` una copia (profunda) de la
    definición de cada action habilitada. Así `fm/adapters/mir/config.py`
    y el driver `sim` no cambian, y el core sigue sin interpretar las
    definiciones (solo comprueba que son mappings). `FleetConfig.actions`
    guarda el catálogo entero, también las actions que ningún robot tiene
    habilitadas, que son válidas: la matriz del configurador las mostrará.
    Errores al arrancar: definiciones dentro de un robot (formato anterior,
    con la pista para migrar), lista con nombres que no están en el
    catálogo o repetidos, y catálogo que no es un mapping.

## 2026-10-14 — Fase (b) del configurador: pestaña en solo lectura

`pytest`: 132 en verde (3 nuevos). Probada con la config real y los dos
robots: solo la parte web, desde un script sin el bucle del FM (sin
auto-carga ni orders), con una comprobación rápida (solo GET) por robot.
Todas las casillas habilitadas salen ✓; `recoger_pieza` en mir-1 sale ✓
atenuado (la mission está grabada aunque no la tiene habilitada).
Comprobada también a 390 px de ancho.

95. **La matriz se calcula en el navegador cruzando dos cosas que ya
    existían**, sin peticiones nuevas al robot: qué recursos del catálogo
    necesita cada action (`GET /api/config`, que lo pide a
    `MirSync.action_needs`: no depende del robot, basta con el primero que
    sincroniza) y el último informe de `SyncMonitor` de cada robot (el de
    la pestaña Sincronización, que llega también por el WebSocket). Así
    las casillas se actualizan solas tras cada comprobación o despliegue.
    `action_needs` es un método opcional del objeto de sincronización,
    como `check`: el servidor web sigue sin saber qué es una mission.
    Cada casilla marca también el estado de las actions **no**
    habilitadas, atenuado: dice si habilitarla exigiría sincronizar antes.
    La pestaña muestra la configuración del arranque; hasta la fase (c),
    para cambiarla se edita `fleet.yaml` y se reinicia el FM.

## 2026-10-14 — Fase (c) v1 del configurador: editar casillas y umbrales

`pytest`: 155 en verde (23 nuevos). Probado con la flota simulada (FM
completo: guardar por API → aplicado en caliente en el tick siguiente, el
`factsheet` retenido de `sim-1` cambia, un `pose` editado a mano se detecta
como "pide reiniciar") y la UI con la config real copiada al scratchpad (solo
la parte web, sin broker): Editar → marcar `recoger_pieza` en mir-1 y bajar
`battery_floor` a 18 → Guardar → "Aplicado en caliente", y en el fichero
solo cambian esas dos líneas.

**Incidente durante la prueba (sin consecuencias):** el FM simulado de
prueba se lanzó mientras el usuario tenía el FM real en marcha (arrancado
unos minutos antes). Los dos usan el client id MQTT `fleet-manager` y se
echaron mutuamente del broker cada 2 s durante ~45 s; una `fleet/order`
de prueba (`dejar`) se publicó en el topic real y se perdió en una de esas
desconexiones (comprobado: ningún MiR recibió order ni se movió;
`fleet/connection` retenido quedó `ONLINE`, del FM real). Las pruebas de la
web se hacen desde ahora sin broker (`WebServer` + `FakeBus`), y antes de
lanzar un FM de prueba se comprueba que no haya otro (`pgrep -af run_fm`).
Pendiente de decidir: client id distinto por instancia (punto 21).

96. **`fleet.yaml` se edita en el texto, sustituyendo solo el valor**
    (`fm/config_edit.py`), con la posición exacta de cada nodo que da
    `yaml.compose` de PyYAML, en vez de `ruamel.yaml` (previsto en el
    plan). Se probó `ruamel.yaml` en ida y vuelta sin editar nada y no
    dejaba el fichero igual: `{ min: 1 }` → `{min: 1}` y la lista partida
    de mir-2 en una línea de 110 caracteres. Así el diff en git es la línea
    cambiada y no hace falta dependencia nueva. Si el valor cambia de
    largo, el comentario de la derecha se recoloca en su columna. Una
    clave que falta se inserta como primera de su bloque, con su sangría
    (o el bloque `auto_charge`/`assigner` al final si falta entero).
    Antes de escribir: validación con el cargador del arranque y con el
    driver de cada robot; luego copia `fleet.yaml.<AAAAMMDD-hhmmss>.bak` y
    escritura atómica (`.tmp` + `os.replace`). `config/*.bak` y
    `config/*.tmp` van al `.gitignore`.
97. **En caliente con el `Reloader` (`fm/reload.py`).** `restart_reasons`
    decide: robots que entran o salen, cualquier clave de un robot que no
    sea `actions`/`battery_min`, el bloque `drivers:` o MQTT → reiniciar;
    lo demás, en caliente. La web encola la config validada y el hilo
    principal la aplica al inicio del tick, toda o nada (primero construye
    los drivers nuevos; si uno falla, no cambia nada). Al robot cuyas
    actions cambian: gancho opcional nuevo del driver `reconfigure(otro)`
    (adopta la config de un driver recién construido, sin red) y
    `Robot.reindex`, que hace que el hilo de poll vuelva a llamar a
    `connect()`; se republica su `factsheet`. Mientras tanto se conservan
    los índices viejos (todas las missions del grupo ya están). Sin
    `reconfigure`, cambiar actions pide reiniciar. `MirSync.wanted` lee la
    config actual del driver, para que la Sincronización siga al día.
    Edición manual de `fleet.yaml` con el FM en marcha: `GET /api/config`
    compara el fichero con la config en uso y la pestaña ofrece "Aplicar
    el fichero" (o dice qué pide reiniciar). El resultado del tick llega a
    la web por el WebSocket (`fm/config`).

## 2026-10-14 — Configurador v2: crear, editar y borrar actions del catálogo

`pytest`: 167 en verde (12 nuevos) más `test_ficheros_del_repo_cargan`,
que falla por el `fleet.yaml` de trabajo del usuario (a medio editar:
`traer` habilitada en los robots pero aún sin definir en el catálogo).
Probado en el navegador con una copia del `fleet.yaml` de HEAD, solo la
parte web y sin broker: Nueva action `traer` → mission `traer` del catálogo
→ el formulario propone `n_piezas_pedidas` como número → rango 1–10–1 →
marcarla en mir-1 (✓; en mir-2 sale ✗ porque no la tiene grabada) →
Guardar → "Aplicado en caliente"; en el fichero, el bloque nuevo al final
del catálogo y la línea de mir-1.

98. **Formulario de actions con las claves del driver MiR** (mission,
    `position_inputs`, `number_inputs` con rango, `required_inputs`,
    `positions_allowlist`). El core sigue sin interpretarlas:
    `fm/config_edit.py` escribe la definición que llega y la valida el
    driver al guardar; el formulario es presentación. Las claves que el
    formulario no conoce se conservan. Al elegir una mission del catálogo
    se proponen sus inputs con el tipo que parecen tener
    (`MirSync.form_choices` / `guess_input_kind`: `id: position` o GUID de
    position → position; valor numérico → número; lo demás → texto); cada
    uno se puede dejar "sin declarar". Una mission fuera del catálogo se
    escribe por nombre, con sus inputs a mano.
99. **Reglas del catálogo:** no se renombra una action (el `actionType` es
    el contrato con el sistema aguas arriba: se crea la nueva y se borra la vieja); solo
    se borra si ningún robot la tiene marcada (la UI lo impide y, si llega
    igual, el cargador lo rechaza al validar); una action nueva se puede
    crear y habilitar en los robots en el mismo "Guardar" (el editor
    aplica primero el catálogo). En el texto, la edición es **clave a
    clave**: solo cambian las claves distintas (los comentarios de las
    demás siguen), una clave nueva va al final de su bloque, una borrada
    se lleva sus líneas, y el comentario `# <nombre>` de `mission` se
    pone al día. Una action nueva se añade al final del catálogo con el
    mismo formato que el resto (bloque, y el rango en flujo).

## 2026-10-14 — Pestaña Crear: missions y positions nuevas con GUID de la flota

`pytest`: 179 en verde (11 nuevos). Probada en el navegador solo la parte
web, con `MirSync` real sobre una **copia** de `catalog/` y el MiR falso de
los tests (nada se creó en los robots): mission «coge 5 contactores» en
mir-1 → `imperialfleet-mission000-01-0001-012` (el siguiente al 011 del
catálogo), borrador en pendientes, capturar → al catálogo; position «Balda
3» → `…-position00-01-0001-026` en la pose del robot; repetir el nombre →
rechazo. Sin probar contra un MiR real: el `POST` con GUID elegido ya se
usa al desplegar, pero falta crear de verdad desde la pestaña y comprobar
que el enlace abre la página de edición.

Comprobado con los robots (solo GET): **el mapa activo de los dos ya es
`suberri_mirur`** (`imperialfleet-map0000000-01-0001-001`), y tiene el mismo
origen (-13.825, -3.275) y resolución (0.05) que `suberri`. El punto 20 de
"Pendiente" estaba desfasado.

100. **Pestaña Crear, la primera del grupo de administración** (decisión
     del usuario): da de alta en UN robot (mir-1 o mir-2, a elegir) una
     mission vacía o una position, con GUID estándar, y manda a la web del
     MiR a terminarla. El camino de un recurso nuevo queda en las
     pestañas: Crear → web del MiR → Capturar → Sincronización →
     Configurador. Rutas de la web del MiR: `/missions/<guid>` (Blockly) y
     `/maps/<guid>` (editor del mapa, donde están sus positions). Primero
     se sacaron del código de su interfaz con el prefijo `/setup`, que da
     404; el usuario comprobó en mir-1 la buena (`/missions/<guid>`), y
     después la de mapas (`/maps/<guid>`): las positions se crean bien y
     el enlace lleva al editor correcto.
101. **Positions donde está el robot** (decisión del usuario): normales
     (tipo 0), en la pose actual, y solo en un mapa del catálogo que sea
     el activo del robot (hoy `suberri_mirur`). Quien no sabe dónde está
     el (0, 0) del mapa la encuentra junto al robot. Los marcadores se
     hacen en la web del MiR convirtiendo la position (el MiR crea su
     entrada y su docking offset); al capturar se recoge también la
     entrada (decisión 77).
102. **El número del GUID se reserva con un borrador en `catalog/`**
     (decisión del usuario): siguiente = máximo de los GUIDs estándar del
     catálogo (borradores incluidos) y de los robots que respondan, + 1;
     al crear se escribe el documento con `"borrador": true`. `Catalog`
     separa los borradores (`drafts`): no se despliegan ni cuentan para la
     Sincronización hasta capturarlos. **Capturar** (en la misma pestaña)
     relee el recurso del robot donde se creó y escribe el documento
     completo; si la mission usa algo que no está en el catálogo (o aún es
     borrador), no se captura y dice qué falta. Nombres repetidos en el
     mismo grupo o mapa se rechazan (romperían la resolución por nombre).
     Código: `fm/adapters/mir/create.py`; la web solo usa la capacidad si
     el objeto de sincronización la ofrece.

**Primera prueba con robot (usuario, 2026-10-14):** creada en mir-1 la
mission `prueba-creacion` (`…-mission000-01-0001-012`), programada en su
web y capturada (2 actions). Desplegarla en mir-2 no se pudo: mir-2 estaba
ejecutando una mission lanzada desde su web (esperando el registro PLC 7),
y la Sincronización no despliega en un robot ocupado. La pestaña no decía
por qué; ahora lo avisa junto al botón. Corregidos también el enlace a la
web del MiR (sin `/setup`) y el contraste de los enlaces dentro de los
avisos de color en modo oscuro.

103. **Desplegar en un robot ocupado: crear sí, corregir no** (decisión
     del usuario). Antes no se desplegaba nada en un robot `busy` (order,
     carga o mission lanzada desde su web). Ahora, si la vista previa solo
     tiene recursos que **crear** (el robot no los tiene: no pueden estar
     en uso), se despliega igual; si hay algo que **corregir** en un
     recurso que ya tiene (y que la mission en curso podría estar usando),
     se bloquea hasta que quede libre. Se comprueba dos veces: el servidor
     con la vista previa antes de empezar y `MirSync.deploy(only_create=
     True)` al replanificar (si entretanto ha aparecido algo que corregir,
     no toca nada). La pestaña dice cuál de los dos casos es.
104. **Borrar lo creado desde la pestaña Crear** (petición del usuario:
     no ir gastando números de GUID con pruebas). Solo lo creado allí,
     borrador o capturado: se reconoce porque su GUID en el robot es el
     nuestro (lo capturado con el script trae el GUID propio del robot).
     Primero un plan (en qué robots está y qué lo impide), confirmación, y
     se borra de esos robots y del catálogo, con la entrada si es un
     marcador. **No se borra** si lo usa `fleet.yaml` (una action, aunque
     no esté habilitada, o la mission de carga), si lo usa otro recurso del
     catálogo, si algún robot no responde (podría guardar una copia, y su
     número se volvería a usar) o si un robot que lo tiene está ocupado.
     Si era el número más alto, la siguiente creación lo reutiliza; un
     hueco intermedio no se rellena. Si el borrado falla a medias, el
     catálogo no se toca y se puede reintentar.

## 2026-10-14 — Quitar de un robot y retirar de la flota

`pytest`: 191 en verde (6 nuevos). Probado en el navegador solo con la
parte web, sobre una copia del catálogo y el MiR falso de los tests. El
usuario probó antes con robot el «Borrar» de la pestaña Crear: bloquea con
el robot ocupado; parando las missions en marcha, borró sus pruebas.

105. **Retirar de la flota guarda el número** (decisión del usuario).
     Cualquier mission o position del catálogo (no la entrada de un
     marcador: va con él) se borra de todos los robots y del catálogo, y
     sus GUIDs se apuntan en `catalog/retirados.json`, que la numeración
     de la pestaña Crear salta. Motivo: lo de la flota tiene historial en
     git y puede seguir en backups del MiR; reutilizar su GUID para otra
     cosa confundiría. El «Borrar» de la pestaña Crear sí libera el número
     (son pruebas). Mismos bloqueos que el «Borrar» (decisión 104), más:
     una position que esté en una `positions_allowlist` de `fleet.yaml`.
     Lo que no lleva GUID `imperialfleet-…` no se toca nunca: se gestiona
     en la web del MiR (capturarlo desde la UI sigue en el punto 19).
106. **Quitar de un robot, solo missions, y los «sobrantes».** La
     comprobación de la flota marca como sobrantes las missions del
     catálogo que un robot tiene y su `fleet.yaml` no pide (ni lo que pide
     las usa); cada una lleva un botón para quitarla de ese robot, y el
     catálogo no cambia (se puede volver a desplegar). No se quita si la
     necesita, si la usa otra mission que el robot tiene, o con el robot
     ocupado. Las positions no son "sobrantes": una order puede pedir
     cualquiera por nombre (`target_pos`), así que el FM no sabe si un
     robot no la usa; una position se retira de la flota o nada. Código
     común en `fm/adapters/mir/remove.py` (también lo usa la pestaña Crear).

## 2026-10-14 — Un solo FM por broker (punto #21)

`pytest`: 193 en verde (2 nuevos). Probado con el Mosquitto local y la
flota simulada (comprobando antes que el usuario no tenía un FM en marcha):
con un FM arrancado, un segundo sale con código 3 y el mensaje con los
topics `ONLINE`; el primero no sufre ninguna desconexión.

107. **Un FM no arranca si ya hay otro conectado al broker.** Antes de
     conectar, `mqtt_bus.running_fm()` lee con un cliente de usar y tirar
     (client id propio: no echa a nadie) los `connection` retenidos de
     `fleet` y de sus robots; si alguno dice `ONLINE`, `run_fm` sale con
     código 3. Un FM parado deja `OFFLINE` y uno muerto `CONNECTION_BROKEN`
     (Last Will), así que un `ONLINE` retenido es casi siempre un FM vivo;
     para el caso raro de uno antiguo, `--force`. **No** se cambia el
     client id a uno único: así los dos FM convivirían sin echarse, pero
     atenderían los dos cada `fleet/order` (el mismo trabajo a dos robots).
     Si no se puede preguntar al broker, decide el arranque normal.

## 2026-10-14 — Configurador v3: altas y bajas de robots, y reinicio desde la web

`pytest`: 202 en verde (9 nuevos). Probado con el Mosquitto local y la
flota simulada (comprobado antes que no había otro FM): alta de `sim-3` por
la API → «requiere reiniciar» → `POST /api/restart` → el mismo proceso (mismo
PID, `os.execv`) vuelve con `sim-3` conectado; desde el navegador, baja de
`sim-3` → Guardar → «Reiniciar el FM ahora» → la página se reconecta sola
con `sim-1` y `sim-2`; y el formulario de alta de un `mir` con su host. Al
probar salieron dos fallos, corregidos: la API no aceptaba `null` (la baja)
en `robots`, y el aviso «requiere reiniciar» se borraba nada más salir.

108. **Altas y bajas de robots desde el Configurador, con reinicio desde la
     web.** En el panel Robots (modo edición): «Añadir robot» (serial,
     driver, host si no es `sim`, batería mínima y actions), cambiar el
     `host` y «Quitar robot». En `fleet.yaml`, como siempre, solo esas
     líneas: el robot nuevo va al final de `robots:` (antes que las bajas,
     para que el bloque nunca quede vacío) y una baja se lleva sus líneas;
     tiene que quedar al menos un robot, y el driver valida el bloque al
     guardar (un `mir` sin host no se guarda). Todo esto es estructural
     (decisión 91): se guarda y la pestaña ofrece **«Reiniciar el FM
     ahora»**, que se adelanta a lo que la decisión 91 dejaba para más
     adelante porque sin él cada alta obliga a ir al terminal. El bucle
     principal sale, cierra limpio (`OFFLINE`) y `run_fm` se relanza con
     los mismos argumentos (`os.execv`); el proceso nuevo pasa la
     comprobación de la decisión 107 porque el anterior dejó `OFFLINE`.
     Antes de reiniciar, la pestaña avisa de los robots que están
     trabajando: el robot termina, pero el FM pierde de vista esa order.

## 2026-10-23 — Pestaña Guía

Pestaña nueva al final del grupo de administración (`#guia`, solo HTML en
`fm/web/static/index.html`, sin backend). Petición del usuario: que quien
coge el FM por primera vez tenga el paso a paso de cada proceso, que hoy
reparte sus pasos entre Crear, la web del MiR, Sincronización y
Configurador.

109. **Guía con un desplegable por proceso** (`<details>`, cerrados por
     defecto): las piezas (mission, position, action, catálogo, borrador),
     añadir y quitar una mission, añadir y quitar una position. Cada paso
     nombra su pestaña con un enlace (`#crear`, `#sincronizacion`…), que es
     lo que aporta frente a un documento en `docs/`. Solo se describen
     procesos que el FM ya hace: uno futuro (p. ej. áreas, #23) entra
     cuando exista, como un `<details class="guide-sec">` más.
     **Regla (decisión del usuario):** todo cambio en el flujo de otra
     pestaña (un botón que cambia de nombre o de sitio, un bloqueo nuevo,
     un paso que sobra o falta) actualiza la Guía en el mismo cambio.
     Está también en el comentario de cabecera de la pestaña.

## 2026-10-23 — Pestaña Robot y secciones arriba

Petición del usuario: una vista de UN robot con lo que la tarjeta de
Operación no cuenta, elegido con un selector arriba; y que las vistas
generales (Robot; más adelante Fleet) vayan en la cabecera, por encima de
las pestañas de la barra lateral, que son del Fleet Manager. Orden
acordado: (1) Robot con lo que ya trae `/status` + el `state` VDA en
crudo; (2) mission en curso con su paso, cola e historial; (3) Fleet con
mapa; (4) cámara.

110. **Lo que el FM no usa para decidir va en `state.information[]`**, con
     `infoType` propios y genéricos (`I_*` en `fm/vda5050/state.py`:
     `ROBOT_NAME`, `ROBOT_MODEL`, `ROBOT_SERIAL`, `ROBOT_STATE`,
     `USER_PROMPT`, `DISTANCE_TO_TARGET`, `ODOMETER`, `UPTIME`,
     `MANUAL_DRIVE`, `SAFETY_MUTED`, `MAP_UNSAVED`). La norma reserva
     `information[]` para "visualización o depuración", que es justo esto, y
     así la UI sigue siendo un cliente VDA más (no hay topic ni endpoint
     nuevo) y el core sigue sin saber de MiR: los rellena
     `adapters/mir/translate.py::to_telemetry` (puro, con tests) a partir de
     campos de `GET /status` que antes se descartaban. Lo vacío o en falso no
     se publica. La velocidad no va ahí: `state.velocity` es campo VDA
     (`vx`, `vy` = 0, `omega`) y entra por `Telemetry.velocity`; un driver
     que no la sabe (sim) no publica el campo.
111. **Dos supuestos sin verificar con el robot** (ver #24):
     `velocity.angular` del MiR en grados/s (como `orientation`), que se pasa
     a rad/s; y `user_prompt` como objeto con `question` y `options`. Si el
     formato no es ese, se publica tal cual en vez de perderlo.
112. **Secciones arriba: Robot | Fleet Manager.** La cabecera lleva las
     secciones generales; la barra lateral (Operación, Crear, Configurador,
     Sincronización, Guía) solo se ve en Fleet Manager, y "Fleet Manager"
     vuelve a la última pestaña lateral usada. Robot va en `#robot/<serial>`
     (enlazable, sobrevive a recargar), con un selector arriba. Enseña:
     aviso destacado si una mission espera respuesta del operario
     (`USER_PROMPT`: explica un robot "parado sin motivo"), avisos de
     joystick / seguridad silenciada / mapa sin guardar, estado y modo,
     mission, distancia al destino, velocidad, posición con orientación,
     batería con umbrales, actions habilitadas, order en curso, identidad
     (nombre, modelo, nº de serie de fábrica), odómetro, tiempo encendido,
     todos los errores (Operación enseña los 3 últimos) y el `state` VDA tal
     cual se publica (material didáctico). Mandar (pausa, cancelar, orders)
     sigue solo en Operación. Los robots siguen siendo `mir-1`/`mir-2`
     (serial VDA, topics, `fleet.yaml`); la UI enseña el nombre de la web del
     MiR (`ROBOT_NAME`: Dejavu, Toretto) y el serial en pequeño, también en
     las tarjetas de Operación (decisión del usuario: no renombrar, para no
     romper topics ni lo que los escucha). Probado con mir-1 y mir-2 reales
     (solo lectura, FM arrancado y parado sin orders) y a 390 px de ancho.

Aparte: `battery_min` de mir-1 y mir-2 está en **22 %** (antes 25; guardado
desde el Configurador el 2026-10-16). Intencionado (usuario, 2026-10-23):
ninguna mission actual gasta tanta batería, así que 2 puntos sobre
`battery_floor` (20 %) bastan de histéresis (§6 del brief).

## 2026-10-23 — Pestaña Robot: misiones en curso, cola e historial (punto #25)

113. **Misiones fuera del `state`, a demanda.** VDA 5050 no tiene historial
     ni pasos de una mission, y meterlo en el `state` de 1 Hz costaría
     peticiones al robot aunque nadie mire. La pestaña pide
     `GET /api/robots/<serial>/missions` cada 3 s, solo mientras está abierta
     y solo para el robot elegido. Capacidad opcional del driver
     `make_missions()` (como `make_sync`): objeto con su propio `MirClient`
     que corre en el hilo de la web, nunca en el bucle. Tipos genéricos en
     `fm/adapters/base.py` (`MissionsView`, `MissionRun`, `MissionStep`;
     `status` con las palabras de `actionStatus`); lo de MiR en
     `fm/adapters/mir/missions.py`. Coste: `GET /mission_queue` trae toda la
     historia (1.482 entradas en mir-1) pero solo id y estado (~0,1 s); el
     detalle de lo terminado se pide una vez y se guarda, y la caché se poda a
     lo que se enseña. Primera consulta 0,47 s, siguientes 0,08 s. Salen
     también las missions lanzadas desde la web o la tablet; "FM" marca las
     que lanzó este FM desde que arrancó (`_launched_ids` del driver).
114. **Reloj de los robots desfasado.** mir-1 iba 2 min 19 s y mir-2 5 min
     27 s atrasados respecto al PC (cabecera HTTP `Date`). Las horas de la
     cola son las del robot: se mide el desfase en cada consulta
     (`clock_offset_s`) y la UI las pasa a la hora del PC, la de los logs del
     FM, con un aviso si pasa de 1 min. No afecta al `state` VDA, cuyo
     `timestamp` pone el FM. Conviene poner en hora los robots (NTP en la web
     del MiR).
115. **Mensajes del MiR con plantilla, legibles en todas partes.** El MiR
     manda algunos textos como JSON con plantilla Python (`%(arg)s`): pasos,
     `message` de la cola y `description` de `errors[]`. Antes llegaban
     crudos a `actionResult`, a `state.errors` y a los eventos; ahora
     `translate.render_message` los rellena (visto con mir-2: "Movement timed
     out, after being blocked a total of 60 seconds").

Prueba con los robots (autorizada por el usuario: `ir_a_espera` y las de
prueba de parámetros): `ir_a_espera` en mir-1 (FINISHED, 41 s; un paso
FAILED dentro de su Try/Catch, que la pestaña enseña tal cual);
`prueba_parametros` (n=3) en mir-1, FINISHED; `prueba_parametros_v2`
(Montaje, n=4) en mir-2: llegó a Montaje, escribió 4 en el registro 22, cargó
"Ir a zona de espera" y se bloqueó 60 s al volver → FAILED y robot en Error
(reseteado a mano en el robot). Con el robot girando se midió la orientación
cada 0,5 s: `velocity.angular` va en **grados/s** (52 tramos, ratio 0,98), así
que el supuesto de la decisión 111 queda confirmado.

Aparte (decisión del usuario, 2026-10-23): `img_referencia/` sale del repo
(capturas de la web del MiR y el PNG de `mapa_base/`). Sigue en el disco del
PC del FM como material local y está en `.gitignore`; las menciones
anteriores de esta bitácora y de `plan-admin.md` se refieren a esa carpeta local.

## 2026-10-23 — Adoptar una mission hecha en la web y sustituir lo distinto (parte del #19)

Caso real: en mir-1 se borró `coger` (`…-mission000-01-0001-001`) y se rehízo
con «Guardar como» otra: la buena tiene GUID del robot (`181aad0a-…`), la
action `coger` de `fleet.yaml` dejó de encontrar su mission en mir-1 (aviso
al arrancar, `NO_ROUTE_TO_TARGET` en una order) y mir-2 y el catálogo
seguían con la versión vieja (2 actions).

116. **Adoptar** (pestaña Crear, «Hechas en la web del MiR»): lista las
     missions de los grupos del catálogo que no tienen GUID de la flota, con
     la del catálogo del mismo nombre y grupo como propuesta, y las guarda en
     el catálogo con el GUID de esa (la sustituye) o con uno nuevo de su
     grupo. Misma captura y mismas comprobaciones que «Capturar»
     (`_capture_mission` con `source`); `_origen.guid` es el GUID del robot,
     y mientras la copia siga en el robot sale como «ya adoptada». No
     escribe en el robot: el MiR no deja cambiar el GUID de una mission, así
     que se despliega la del catálogo y la copia se borra a mano en su web
     (decisión 105: lo que no lleva GUID de la flota no se toca).
117. **Sustituir** (Sincronización): una mission *distinta* lleva una casilla
     «Sustituir»; marcada, la vista previa la pasa a `sustituir` y el
     despliegue la borra y la vuelve a crear con el mismo GUID. Solo
     missions (positions y mapas distintos siguen sin tocarse). Con el robot
     ocupado se niega, como corregir (decisión 103). Probado en mir-2 con
     missions temporales antes de escribirlo: el MiR deja borrar una mission
     aunque otra la cargue, el `load_mission` conserva el GUID y vuelve a
     valer al recrearla, así que si otra la carga solo se avisa; y al borrar
     una mission el MiR borra sus actions (al recrearla sale vacía).
     Guía: proceso nuevo «Cambiar una mission que ya está en la flota»
     (editar en el sitio, sin «Guardar como»; y qué hacer si se usó).

Al aplicarlo a `coger` (adoptada en el catálogo; nada desplegado todavía):
la vista previa mostró que la nueva atraca en MESA_TKNIKA (`…-024`) y que en
mir-1 ese marcador está ~4,7 m y ~180° lejos de lo que dice el catálogo
(con otro docking offset), y que su entrada se "corregiría" a la pose vieja.
El compañero seguía editando en mir-1 (una hora antes `coger` atracaba en
H2D2-VL), así que se paró: falta volver a adoptar `coger` y recapturar
MESA_TKNIKA de mir-1 cuando lo dé por bueno, y entonces desplegar.

Seguimiento (mismo día): `coger` definitiva readoptada de mir-1 y
desplegada en mir-1 con su GUID de la flota (solo "crear"; idéntica action
a action a la copia). Para que el despliegue no "corrigiera" la entrada de
MESA_TKNIKA a la pose vieja del catálogo, antes se recapturó MESA_TKNIKA
(`…-024` y su entrada `…-025`) de mir-1. MESA_TKNIKA es un marcador nuevo
que el compañero aún está colocando: **punto vivo**, se recaptura de mir-1
cada vez que cambie (decisión del usuario).

118. **Por GUID se encuentran todas las missions, aunque se repita el
     nombre.** Tras desplegar, el FM seguía sin ver `…-001` en mir-1: el
     índice era nombre → GUID con "el primero gana", la copia del «Guardar
     como» salía antes y el GUID de la flota ni entraba. Ahora el driver
     indexa por GUID todas las missions del grupo; por nombre, sigue el
     primero con su aviso (`client.group_missions`, test con el caso real).

## 2026-10-23 — Crear VL-markers y cargadores desde la pestaña Crear (punto #29)

El usuario comprobó en la web del MiR que el tipo de una position no se
puede cambiar una vez creada (el selector "Select position type" solo sale
al crearla), así que el camino de la decisión 101 (crear normal y
convertirla en la web) no vale. Sí deja, en un VL-marker ya creado, volver
a detectar el marcador y cambiar pose y offset.

119. **El tipo se elige al crear** («Nueva position» → Tipo). Tabla
     `POSITION_TYPES` en `fm/adapters/mir/create.py` con los tres que usa hoy
     la flota y ya se crearon por la API al montar el catálogo: Robot
     position (0), VL-marker (11) y MiR Charge 48V (20). Decisión del
     usuario: solo esos; otro tipo (V, L, Bar, Precision marker…) es una
     fila más y una prueba con el robot cuando haga falta (el robot da la
     lista completa en `GET /position_types`). Un VL-marker se crea con
     GUID de la flota, en la pose del robot y con un docking offset
     provisional (x −0,8 m, como los del catálogo; la API lo exige), y el
     MiR le pone su entrada; en la web se vuelve a detectar el marcador y se
     ajusta el offset, y «Capturar» recoge marcador, offset y entrada
     (decisión 77). Test nuevo de esa captura (lo que pedía el #29). Guía
     actualizada («Añadir una position nueva»).

## 2026-10-29 — Revisión de `docs/sugerencias_mejora.md`

Una revisión externa propuso mejoras de fiabilidad y funciones nuevas. Se
apuntan como pendientes solo las necesarias; el resto queda en el
documento de sugerencias, sin compromiso.

- Pasan a pendientes: **#30** (resuscribir al reconectar), **#31**
  (registro de `fleet/order` aceptadas) y **#32** (acotar `order_errors`).
  #30 se comprobó en el código: `Dispatcher.subscribe()` solo se llama al
  arrancar y `MqttBus._on_connect()` no vuelve a suscribir.
- Se quedan en sugerencias: el presupuesto de mensajes por tick en
  `Dispatcher.drain()` (con 2 robots y un solo emisor no hay ráfagas que lo
  justifiquen y añade latencia), el modo mantenimiento, el indicador de
  antigüedad del estado y el informe de diagnóstico.
- La revisión decía que la suite se quedaba colgada. No se reproduce: 222
  tests en verde en 2,3 s con el comando de CLAUDE.md.

## 2026-10-29 — Resuscribir al reconectar con el broker (punto #30)

El FM se suscribía a `order`/`instantActions` una sola vez, al arrancar.
Con `clean_session=True` el broker olvida las suscripciones al cortarse la
conexión, y paho reconecta solo pero no las rehace: tras reiniciar
Mosquitto, el FM volvía a publicar `ONLINE` y `state`, pero ninguna order
le llegaba.

120. **`MqttBus` apunta las suscripciones y las renueva en cada conexión**
     (`_on_connect` del cliente principal, antes de publicar `ONLINE`). Se
     mantiene `clean_session=True`: con sesión persistente el broker
     entregaría al reconectar las orders recibidas durante el corte, que
     quien las mandó ya no espera (no tuvo ACK) o ya ha reenviado.
     Tests offline nuevos en `tests/test_mqtt_bus.py`. Prueba con
     Mosquitto en un puerto aparte (18830) y `config/fleet-sim.yaml`:
     parar el broker, arrancarlo de nuevo y mandar `fleet/order` →
     `ASSIGNED`. Con el código anterior, la misma prueba se queda sin
     `order_response`.

## 2026-10-29 — Acotar los rechazos de order en el `state` (punto #32)

Cada rechazo de una `<serial>/order` se añadía a `OrderTracker.order_errors`,
que solo se vacía al aceptar otra order. Un emisor que reintenta contra un
robot ocupado hacía crecer `state.errors[]` sin límite.

121. **Rechazos sin repetir y acotados.** Un rechazo idéntico a uno ya
     guardado (misma order y mismo motivo) no se duplica: pasa al final como
     el más reciente. Se conservan los `MAX_ORDER_ERRORS = 10` últimos
     (`fm/orders.py`). Se sigue vaciando todo al aceptar otra order
     (decisión 2). No afecta a `fleet/order`: sus rechazos van en
     `order_response`, no al `state`. Test nuevo en `tests/test_orders.py`;
     README actualizado.

## 2026-10-29 — Validación con schemas en runtime: `--validate` (punto #6)

El brief (§10.1, H6) pedía validar en runtime lo que entra contra los JSON
Schema oficiales; `fm/vda5050/schemas.py` existía pero solo lo usaban los
tests. `parse_order` es más tolerante que el schema: p. ej. acepta una
action sin `blockingType`. Se comprobó que lo que mandan la web y
`scripts/_mqtt.py::build_order` ya valida.

122. **`--validate` en `run_fm.py`, desactivado por defecto** (como en el
     brief). Con la opción, `Dispatcher.handle` valida cada `order`,
     `fleet/order` e `instantActions` contra `schemas/` después del
     header ≡ topic y antes de procesarlo; si no valida, es el mismo
     rechazo `VALIDATION_FAILURE` de siempre (`order_response`,
     `state.errors[]` o error de instantActions), con los 3 primeros
     fallos y su ruta en el motivo. Por defecto queda apagado para no
     cambiar lo que hoy se acepta; activarlo al integrar el emisor de
     `fleet/order` (#2) dice si cumple la norma, y se podría dejar siempre activo
     después. Tests nuevos en `tests/test_fleet.py`. Prueba con Mosquitto
     en el puerto 18830 y `fleet-sim.yaml`: `send_fleet_order.py` →
     `ASSIGNED`; la misma order sin `blockingType` → `REJECTED
     VALIDATION_FAILURE` con `nodes/0/actions/0: 'blockingType' is a
     required property`.

## 2026-10-29 — Avisos por tiempo anómalo, paso 1 (punto #5)

Los datos de la historia de los robots (en el #5, "Hecho con robot") dicen
que lo normal se repite mucho y lo raro es mucho más largo. Paso 1 y la
herramienta del paso 2.

123. **`expected_s` por action del catálogo, genérico.** Es la única clave
     de una definición que lee el core (`fm/config.py` la valida: número
     > 0; cada `RobotConfig` lleva el de sus actions). `Robot.poll` llama a
     `OrderTracker.watch_duration` cada tick: suma el tiempo de la order
     activa en `RUNNING` sin pausa (`Telemetry.paused`), así que la espera
     en cola detrás de una carga no cuenta; un hueco sin respuesta del
     robot sí cuenta, porque lo último que se sabía es que trabajaba. Al
     superar el umbral, un solo aviso `ORDER_DURATION_EXCEEDED` (propio,
     `errorLevel: WARNING`, referencias a la order y la action) que va en
     `state.errors[]` solo mientras la order está viva: se va al terminar o
     cancelarse, sin limpiarlo a mano. La web ya resalta los WARNING en la
     tarjeta del robot y en Operación. El log del cambio de estado al
     terminar dice cuántos segundos estuvo en ejecución. Se cambia en
     caliente desde el Configurador (campo nuevo en el formulario de la
     action; Guía actualizada). Herramienta del paso 2:
     `scripts/mission_durations.py <serial>`, solo lectura, propone
     p90 × 1,5 redondeado a 10 s con al menos 5 ejecuciones terminadas
     bien (con los datos de hoy ninguna action del FM llega: `coger` 2 en
     mir-1). `config/fleet-sim.yaml` pone `expected_s: 5` en `dejar` para
     ver el aviso en la simulación. Probado con la flota simulada en un
     Mosquitto aparte: aviso a los 5 s, sin repetirse, y fuera al terminar
     (7 s en ejecución). En `config/fleet.yaml` no se pone ninguno: el
     valor lo decide el usuario.

## 2026-10-29 — Zonas del mapa desde la pestaña Crear (punto #23)

Igual que una position: se crea aquí con GUID de la flota, se termina en el
editor del mapa del MiR y se captura al catálogo, desde donde Sincronización
la lleva a los demás robots. Antes de escribir código se probó la API en
mir-2 sobre el mapa `suberri`, que en ese momento no era el activo; todo lo
creado se borró al momento. **No debió hacerse ahí:** `suberri` es el mapa
de trabajo de otros proyectos y no se toca nunca (CLAUDE.md). Las pruebas
van en el mapa de la flota o en uno de pruebas.

- `POST /zones` conserva el GUID elegido. Pide `name`, `type_id`, `map_id`
  y `polygon` (vértices en metros del mapa); `shape_type` (`shape` o
  `stroke`, un trazo con `stroke_width`), `direction` y `actions` tienen
  valor por defecto. `GET /maps/<guid>/zones` las lista por mapa.
- Tipos (`type_id`, sacados del JavaScript de la web del MiR): 203
  direccional, 204 preferida, 205 no preferida, 206 prohibida, 208 de
  acceso; con acciones, 101 planificador, 102 sonido y luz, 103 límite de
  robots, 104 velocidad, 105 módulo E/S y 106 evacuación. El 207 da "Zone
  type not found".
- Las acciones van con el formato de la web: `{"type_id": 1, "parameters":
  [{"name": "desired_speed", "value": "0.3"}]}` (valores en texto). Con el
  `action_type`/`id` de `GET /area_events/action_definitions` el MiR da
  500. Les pone un GUID a cada acción y a cada parámetro.
- `PUT /zones/<guid>` cambia polígono, nombre y acciones, pero **ignora
  `type_id`**: como en las positions, el tipo se elige al crear.
- En el mapa de la flota (`suberri_mirur`) no había ninguna zona: las 5 de
  los robots (Baños, Salida Suberri, Vallas…) son del mapa `suberri`.

124. **Zonas en el catálogo (`catalog/zones/`), con GUID `zone000000` por
     mapa** (la fila ya estaba en `docs/plan-admin.md` §4). Se guardan
     tipo, forma, polígono y acciones sin GUIDs (`normalized_zone`). Van
     después de los sonidos al desplegar, porque una zona de sonido usa uno,
     y sus dependencias son su mapa y los sonidos del catálogo que use.
     **Crear** (`create_zone`) pone un cuadrado de 1 m con su centro 2 m
     delante del robot, en su mapa activo. Delante y no debajo: una zona
     prohibida bajo el robot le impediría planificar. Tipos ofrecidos
     (`ZONE_TYPES`): 206, 205, 204, 203, 208, 104, 102 y 101; fuera, 103 y
     106 (son de MiR Fleet) y 105 (apunta a un módulo E/S propio de cada
     robot). **Capturar** rechaza una acción que use un GUID que no es de
     fábrica ni del catálogo (p. ej. un sonido subido solo a ese robot),
     igual que en las missions. **Sincronización:** una zona distinta no se
     toca salvo que se marque «Sustituir», como una mission; entonces se
     corrige en su sitio con PUT o, si el tipo es otro, se borra y se crea
     con el mismo GUID. Con el robot ocupado no se sustituye (decisión
     103). «Borrar» y «Retirar de la flota» valen también para zonas.
     Guía: «Añadir una zona» y «Cambiar o quitar una zona». Tests nuevos en
     `tests/adapters/test_mir_create.py` y `test_mir_sync.py`. Prueba con
     los robots, sobre una copia del catálogo en el scratchpad: zona no
     preferida creada en mir-1, editada por PUT, capturada, desplegada en
     mir-2 con el mismo polígono, renombrada en mir-1, «distinta» en mir-2
     y corregida con «Sustituir»; borrada de los dos a los 3 s. La
     interfaz no se ha probado aún con el FM real (hay que reiniciarlo).

Después, el usuario la probó desde la interfaz ("la creación funciona bien")
y pidió dos cosas:

125. **Qué es cada tipo de zona, al elegirlo.** Debajo del selector de tipo
     sale un resumen de la ayuda de la web del MiR (sacada de su
     JavaScript, en castellano), en `ZONE_TYPES[tipo]["help"]`. **En
     «Creado desde aquí», un selector «Mostrar»** (todo, missions,
     positions, zonas), con cuántos hay de cada tipo; solo salen los tipos
     que tienen algo.

## 2026-10-29 — Subir un mapa al catálogo desde la pestaña Crear

El usuario: el FM tiene que poder subir un mapa, porque aparece una zona
nueva en la planta o porque alguien despliega el FM por primera vez; "el
principio es el mapa + site". Acordado en tres partes: (1) subir un PNG del
PC, (2) capturar un mapa que ya está en un robot, con sus positions y zonas,
y (3) copiar un mapa del catálogo con sus positions y zonas (para
`mapa-pruebas`, copia de `suberri_mirur`). Ante una zona nueva de la planta
se crea **otro mapa** en el mismo site, no se sustituye la imagen del que
está en uso; las transiciones entre mapas, el día que haga falta.

126. **Parte 1: «Nuevo mapa» en Crear** (`create.create_map`,
     `POST /api/create/map`). Nombre, site del catálogo o uno nuevo, PNG,
     resolución (m/píxel, 0,05 por defecto) y origen (esquina inferior
     izquierda, en metros). **Solo escribe en el catálogo**, completo y sin
     borrador (`maps/<guid>.json` + `.png` y, si el site es nuevo,
     `sessions/<guid>.json`): no depende de ningún robot, y llega a ellos
     desplegándolo en Sincronización, que ya sabía crear sessions y mapas.
     GUIDs numerados en la instalación (§4 de `plan-admin.md`), mirando
     también los de los robots que respondan. **El PNG tiene que venir en
     gris de 8 bits** (se lee la cabecera IHDR, sin dependencias): el MiR
     guarda `base_map` tal cual, sin convertir (decisión 76), y así lo
     exporta su web; convertir uno en color pediría Pillow y decidir
     umbrales, y no hace falta de momento. El mapa sale en «Creado desde
     aquí» (sin «Capturar») y «Borrar» se lleva también su PNG; no se deja
     si tiene positions o zonas, o si es el mapa activo de algún robot.
     Guía: «Subir un mapa nuevo». Tests nuevos en `test_mir_create.py`.
     Probado con la web sobre un robot simulado y un catálogo temporal:
     subir el PNG de `suberri_mirur` da 1053 × 631 px = 52,65 × 31,55 m.

## 2026-10-29 — Crear: una sección cada vez

El usuario: con cuatro formularios en paralelo (mission, position, zona,
mapa) la pestaña ya era incómoda en una pantalla pequeña, y no escala al
añadir otro tipo.

127. **«¿Qué quieres crear?» arriba**, con botones Mapa · Position · Zona ·
     Mission (en ese orden: primero el mapa), y debajo un solo formulario.
     Cada tipo trae su frase y sus pasos (`CREATE_KINDS` en `index.html`:
     un mapa no pasa por la web del MiR ni por Capturar). El panel Robot
     solo sale en los tipos que lo necesitan, y «Hechas en la web del MiR»
     (adoptar) solo con Mission. La elección se recuerda en el navegador
     (`localStorage`, con `try`: si no se puede, se empieza por Mapa). Un
     tipo nuevo es una entrada más en `CREATE_KINDS` y un `<template v-if>`.
     Guía: cada proceso dice qué botón elegir. Revisado con un Chromium sin
     ventana a 1300 y 390 px de ancho: sin errores en consola ni scroll
     horizontal.

## 2026-10-29 — Crear un site, y «Acabo de llegar» en la Guía

El usuario: el site (session del MiR) es el entorno donde se crean los
mapas, y para quien empieza con el FM es lo primero que hay que crear. Y
la Guía necesita un "acabo de llegar, ¿por dónde empiezo?".

128. **«Site» es el primer botón de Crear** (`create.create_session`,
     `POST /api/create/session`): nombre y descripción, solo en el
     catálogo, como el mapa. «Nuevo mapa» ya no crea sites: elige uno del
     catálogo y, si no hay ninguno, lo dice con un enlace a «Site» (la API
     de subir un mapa sigue admitiendo `new_session`). Con el catálogo sin
     sites, Crear empieza en «Site». Un site sale en «Creado desde aquí» y
     se puede borrar mientras no tenga mapas ni missions. En la interfaz se
     dice **site**, como en la web del MiR (en Sincronización, «Sites»);
     en código y en el catálogo sigue siendo `session`. **Guía:**
     «Acabo de llegar: ¿por dónde empiezo?» abre la lista: robots
     (Configurador, hace falta al menos uno, porque el catálogo se lee a
     través de ellos), site, mapa, Sincronización, activar el mapa en la
     web del MiR, positions y zonas, missions, actions y probar. «Las
     piezas» define site, mapa y zona. **Hueco que queda a la vista:** una
     mission nueva necesita un grupo de missions del catálogo, y eso no se
     crea desde el FM.

## 2026-10-29 — Site de la flota (fase 1: operación)

El usuario: el FM tiene que saber en qué site está, porque cambiar de site
cambia de mapa y las missions dependen de positions de un mapa concreto.
Decidido: el site es **de la flota** (en `fleet.yaml`, elegido en la
cabecera), y si un robot está en un mapa de otro site **el FM avisa**; el
cambio de mapa se sigue haciendo en la web del MiR.

129. **`site:` en la raíz de `fleet.yaml`** (GUID de un site del catálogo;
     sin él, no se comprueba nada, como antes). El driver dice en qué site
     está el robot: `Telemetry.site_id`/`site_name` (genéricos; en MiR, la
     session del mapa activo, con `GET /maps/<id>` y `GET /sessions/<id>`
     una vez por mapa). Un robot con otro site (`Robot.outside_site`):
     no está disponible para el asignador (con el motivo, no el genérico),
     rechaza las `<serial>/order` con `MOBILE_ROBOT_NOT_AVAILABLE` y lleva
     en su `state` un `ROBOT_OUTSIDE_SITE` (propio, WARNING), que la web
     resalta en su tarjeta. Si el driver no sabe el site (`None`), no se
     compara. **La auto-carga no mira el site:** un robot con poca batería
     va a cargar aunque esté fuera. Se cambia en caliente con el selector
     «Site» de la cabecera (confirmación, `POST /api/config` con `site`;
     se escribe al final de `fleet.yaml` con el nombre de comentario);
     «— sin elegir —» lo quita. `/api/fleet` da el site y los del catálogo.
     Tests en `test_robot_state.py`, `test_config_edit.py`, `test_web.py`
     y `test_mir_driver.py`. Leído de los robots (solo GET): **los dos
     están ahora en el mapa `suberri`** (sites «Suberri» y «suberri», con
     GUID propio de cada robot), así que con `suberri_mirur` como site de
     la flota ninguno recibiría orders hasta cambiar su mapa. Guía: paso
     «Elegir el site de la flota» en «Acabo de llegar».

## 2026-10-29 — Site de la flota (fase 2: administración)

130. **Crear, Sincronización y Configurador enseñan lo del site de la
     flota.** Cada recurso del catálogo dice su site (`Catalog.site_of`):
     el suyo un site; `session_id` un mapa o una mission; el de su mapa una
     position o una zona. Grupos de missions y sonidos no tienen site (son
     de toda la flota) y salen siempre. Va en lo que ya mandaba el servidor
     (`summary`, `create_choices`, `form_choices`, `action_needs`) y filtra
     la interfaz: en Crear, los mapas donde crear positions y zonas y
     «Creado desde aquí» («Nuevo mapa» propone el site de la flota, pero
     deja elegir otro); en Sincronización, las secciones del catálogo; en
     el Configurador, las actions cuya mission es de otro site, las
     missions del formulario y las positions de la allowlist (por nombre,
     con `position_sites`). Una línea bajo el título de cada pestaña dice
     qué site se enseña, con «Ver todo el catálogo». Sin site elegido se ve
     todo, como antes. Comprobado con un Chromium sin ventana y dos sites:
     el mapa del otro site no sale para crear zonas ni en «Creado desde
     aquí», y sí con «Ver todo».

## 2026-10-29 — Una mission con positions fijas va atada a su mapa

Al plantear la estructura del catálogo (se queda igual: una carpeta por
tipo y un fichero por GUID; la jerarquía site → mapa → positions ya está
en los campos y en el GUID, y la interfaz la enseña) salió lo que sí
importa: **7 de las 12 missions del catálogo llevan positions fijas** de
`suberri_mirur` (`coger` y `traer` → MESA_TKNIKA, la carga → su estación,
`Ir a zona de espera MIRUR` → sus tres puntos), y otras las heredan de la
sub-mission que cargan. Esas missions no son de un site sino de un mapa: en
`mapa-pruebas` (mismo site, otras positions con otros GUIDs) no valen, y
sincronizar no lo arregla (copia el mismo GUID). Las que reciben la
position como input (`target_pos`) valen en cualquier mapa, porque el FM
resuelve el nombre en el mapa activo.

131. **Mapas de una mission = los de sus positions fijas y los de las
     sub-missions que carga** (`load_mission`); un input no ata. Lo calcula
     el driver MiR con lo que lee del robot al conectar (en la misma
     lectura de `missions/<guid>/actions`, más `GET /positions` y
     `GET /maps` una vez), así vale también para missions que no están en
     el catálogo: `translate` rechaza con `NO_ROUTE_TO_TARGET` si el mapa
     activo no es uno de ellos, y en `fleet/order` el asignador prueba el
     siguiente robot (decisión 89). El catálogo lo calcula igual
     (`Catalog.maps_of`) para enseñarlo: «· mapa suberri_mirur» en
     Sincronización y en cada action del Configurador. Esta comprobación no
     depende de elegir site. Leído de los robots: con los dos en `suberri`,
     se rechazarían `coger` (en mir-1; la de mir-2 ya no lleva positions
     fijas), `abrir_puerta`, `ir_a_espera` y `recoger_pieza`.
     **Comprobado por el usuario con mir-2** (en `suberri`, una mission de
     `suberri_mirur`, cola 627): el MiR la empieza y la aborta en el mismo
     segundo con «Unable to find a way to get from start to goal in
     different map», y **el robot queda en Error** (código 10101), que hay
     que quitar a mano en su web. Rechazarla antes evita eso.

## 2026-10-29 — Capturar un mapa de un robot

Para quien monta el FM con un mapa ya hecho en un robot (o graba uno nuevo
con él). Leído antes de escribir: `GET /maps` no dice el site de cada mapa
y `GET /maps/<guid>` trae la imagen entera; `GET /sessions/<guid>/maps` da
los mapas de cada site sin imagen. En los robots hay mapas sin GUID de la
flota además de `suberri` (`g6` en mir-1, `G2` en mir-2, de otros
proyectos), y `mapa-pruebas` ya está desplegado en los dos.

132. **«Mapa» → «Capturar de un robot» en Crear** (`create.capture_map`,
     `GET /api/create/<serial>/maps`, `POST …/capture-map`). Lista los
     mapas del robot sin GUID de la flota (ni de fábrica), por site, con
     cuántas positions y zonas tienen. Capturar copia al catálogo, con
     GUIDs de la flota, la imagen con su resolución y origen, las positions
     (con su docking offset y la entrada de marcadores y cargadores,
     `creada_por_mir`) y las zonas, al site elegido o a uno nuevo (por
     defecto, con el nombre del site del robot). **Solo lee el robot.**
     Como al adoptar una mission (decisión 116), el MiR no deja cambiar el
     GUID, así que la copia del catálogo llega por Sincronización (también
     a ese robot, que tendrá las dos) y se activa en su web. **Volver a
     capturar el mismo mapa** (se reconoce por `_origen`) actualiza su copia
     con los mismos GUIDs, numera lo nuevo y avisa de lo que ya no está en
     el robot sin borrarlo. Una zona cuya acción usa un GUID que no es de
     fábrica ni del catálogo (un sonido subido solo a ese robot) se salta y
     se avisa: no se pierde el mapa por una zona. Guía: «Capturar un mapa
     de un robot», y el paso del mapa en «Acabo de llegar». Probado con
     mir-1 sobre una copia del catálogo en el scratchpad (solo GET): `g6`
     entra con 2 positions y 2 zonas en un site nuevo, y recapturarlo
     conserva los GUIDs. Tests nuevos en `test_mir_create.py`.

Al probarlo el usuario (llevar `g6`, de mir-1 y su site «Suberri», a
`suberri_mirur` con sus positions y zonas), dos ajustes: en Sincronización
los mapas salen **por site** («Mapas de suberri_mirur»), porque en una sola
sección «Mapas» no se veía dónde había quedado; y **marcar un mapa marca sus
positions y zonas** (desmarcarlo las desmarca), porque lo normal es llevarlo
entero y la clausura de un mapa solo arrastra su site. Un fallo de la
primera versión: con mir-2 la lista daba `KeyError: 'guid'` por el mapa
`suberri_mirur`, capturado a mano en la fase 0 sin el GUID de origen en
`_origen`; ahora esos no cuentan como "ya capturados".

## 2026-10-29 — Retirar un mapa de la flota

Tras probar la captura con `g6` (llevado a `suberri_mirur` y desplegado en
los dos robots), el usuario lo borró a mano en la web de cada robot y se
comprobó leyendo: **el MiR borra con el mapa sus positions y sus zonas**.
Los ficheros de la prueba se quitaron del catálogo (nunca se commitearon).

133. **«Retirar de la flota» vale también para mapas**, y se llevan sus
     positions (con las entradas de los marcadores) y sus zonas, en los
     robots y en el catálogo, con sus números reservados en
     `retirados.json`; también su PNG. No se deja si es el mapa activo de
     algún robot, si una mission va a alguna de sus positions, si una
     position que solo está en ese mapa sigue en una allowlist de
     `fleet.yaml`, o con un robot que lo tiene ocupado o sin conexión.
     «Borrar» en Crear (mapas subidos allí) sigue la misma regla, en vez de
     bloquearse por tener positions o zonas. La confirmación dice qué se va
     con él (`with` en el plan de `POST /api/retire/preview`). Guía:
     «Quitar un mapa». El original de un mapa capturado (sin GUID de la
     flota) no se toca. Tests en `test_mir_remove.py`; `FakeMir` borra ya
     las positions y zonas con su mapa, como el robot.

## 2026-10-29 — Crear un grupo de missions

Cierra el #34: sin esto, una flota nueva no podía crear su primera mission
(no había ningún grupo en el catálogo).

134. **«Grupo» en la pestaña Crear** (`create.create_group`,
     `POST /api/create/group`): un grupo es solo un nombre y es de toda la
     flota (no de un site), así que, como el site, **se escribe solo en el
     catálogo** (`…-missiongrp-01-0000-NNN`, numerado en la instalación) y
     no necesita robot. **Llega a un robot con su primera mission:** al
     crear una mission en un grupo del catálogo que el robot aún no tiene,
     se crea en él con su GUID (antes había que desplegarlo antes en
     Sincronización; para un registro con solo un nombre era un paso de
     más). **La session de la mission** sale de las demás missions del
     grupo, como antes; en un grupo sin missions, del **site de la flota**
     (`site:` en `fleet.yaml`); sin él, error que pide elegirlo en la
     cabecera. Lo mismo al adoptar una mission en un grupo vacío.
     **Borrar** un grupo creado aquí (libera el número) no se deja si tiene
     missions: en borrador, capturadas o hechas en la web de algún robot
     (`remove.in_use_on`; no se sabe si el MiR las borraría con él). Retirar
     de la flota sigue sin admitir grupos. Guía: «Acabo de llegar»,
     «Añadir una mission nueva» y «Las piezas». Tests en
     `test_mir_create.py` y `test_web.py`.

## 2026-10-29 — Los datos de la instalación salen del repo

El repo del FM es público y quien lo clone no tiene nuestros robots ni
nuestras positions: `catalog/` y `config/fleet.yaml` no le sirven.

135. **`fleet.yaml` y `catalog/` son de cada instalación y viven fuera del
     repo del FM.** Los nuestros pasan al repo privado
     `mirur-instalacion` (al lado de `fleet-manager-mirur`, en GitHub
     `jam90/mirur-instalacion`) con su historial, filtrado del de este
     repo; allí `fleet.yaml` va en la raíz. El FM los encuentra con
     `FM_CONFIG` y `FM_CATALOG` en `.env` (`fm.config.install_paths`,
     rutas relativas a la raíz del FM; `--config`/`--catalog` mandan); sin
     ellas, `config/fleet.yaml` y `catalog/` del repo, ahora en el
     `.gitignore`. Para quien empieza: `config/fleet.example.yaml`, con
     nombres inventados, que es también lo que leen los tests que antes
     leían el `fleet.yaml` real. Sin `fleet.yaml`, `run_fm.py` sale con un
     mensaje que dice qué copiar. Lo publicado antes sigue en la historia
     del repo público: no se reescribe (IPs privadas y poses de un taller;
     no compensa un force push).

## 2026-10-29 — Vuelta a la espera tras un rato parado

En pruebas con otro FM, algunas missions dejan al robot en un sitio de paso
y molesta al otro. El usuario pide que, pasado un tiempo sin trabajo, el FM
mande a los robots a la espera, con el tiempo y la mission configurables.

136. **`idle_return`: tras `after_s` segundos parado, el FM lanza una
     action del catálogo** (`fm/idle.py::IdleGuard`, en el tick de cada
     robot como la auto-carga). Es una action y no una mission suelta
     para reutilizar todo lo de una order (parámetros, positions por
     nombre, mapa de la mission) y no depender de la marca; tiene que
     estar habilitada en el robot. Se define para la flota y cada robot
     puede usar otra (`{action, after_s, params}`, lo que no diga se
     hereda; con otra action no hereda los params) o ninguna (`false`).
     **Parado** (criterio del usuario): disponible, en modo automático,
     sin pausa, en el site de la flota y sin order, carga ni mission de la
     web; si deja de cumplirse, la cuenta vuelve a cero. **Una vez:** no
     se repite hasta que el robot haga otra cosa (si no, en la espera
     seguiría parado y entraría en bucle); al arrancar el FM cuenta como
     recién hecho, así que un robot parado se va a la espera. **No ocupa al
     robot** (elegido por el usuario): el asignador lo puede elegir
     mientras va y `Robot.execute` cancela antes la vuelta, para que la
     order no espere detrás. Si no se puede mandar o acaba FAILED, log y no
     se insiste. `information` `IDLE_RETURN`; la web lo pinta «a la
     espera». En caliente (`HOT_ROBOT_KEYS`) y editable en el Configurador
     (panel «Vuelta a la espera»; `config_edit` escribe el bloque de la
     flota clave a clave y el del robot en una línea). Guía: «Que un robot
     parado no estorbe». Sin probar con robot (#35).

137. **El contador de la vuelta a la espera se ve en la web** (pedido por
     el usuario): mientras cuenta, el `state` lleva `information`
     `IDLE_RETURN_IN` con los segundos que faltan (enteros, como texto,
     igual que `DISTANCE_TO_TARGET`), y la pestaña Robot («Ahora») y la
     tarjeta de Operación enseñan «A la espera: en 12 s si sigue parado»
     o «yendo ahora». Va en el `state` y no en una API aparte para que lo
     vea también cualquier cliente MQTT. En Operación la fila solo sale
     mientras cuenta o va.

## 2026-10-30 — Pruebas con robot

- **#11 Robot que se enciende con el FM en marcha: hecho.** El FM arrancó
  con los dos MiR apagados y los cogió solos al encenderlos. El usuario lo
  ve también a diario: arranca el FM sin la VPN del taller y, al
  conectarla, los dos robots se conectan sin tocar nada.
- Al encender un MiR hay que pulsar el botón azul físico. El Error 10101
  de mir-2 siguió tras apagarlo y encenderlo: el usuario lo quitó a mano.
- Un robot en Pause cuenta como disponible a propósito: se reanuda desde
  el FM (`stopPause`), sin ir al robot.

## 2026-10-30 — La vuelta a la espera mira la zona

Primera prueba con robot de la #35: al reiniciar el FM, mir-2 ya estaba en
la zona de espera y salió a hacer la mission igual (al arrancar, el FM da a
todos por «recién hecho algo»). El usuario pide que dependa también de la
posición: de la zona de espera que ya hay en el mapa.

138. **`idle_return.zone`: solo se manda a la espera a un robot que esté
     fuera de esa zona del mapa.** Dentro, ni cuenta ni va (no hay
     contador), y no gasta el «una vez»: si lo sacan de la zona sin hacer
     nada más, cuenta y vuelve. La zona es la del mapa (elegida por el
     usuario frente a una lista de positions con un radio): ya existe,
     cubre los dos puntos de espera y se ve en la web del MiR. Se guarda
     por GUID (el del catálogo es el mismo en toda la flota y sobrevive a
     un cambio de nombre) y el driver la busca en el mapa activo por GUID
     y, si no, por nombre. Gancho opcional `in_zone(zone, telemetry)` del
     driver (`fm/adapters/base.py`); el de MiR lee el polígono del robot
     (`maps/<mapa>/zones`, `zones/<guid>`) desde el hilo de poll y lo
     guarda 60 s (`ZONE_TTL_S`), así que redibujarla en la web se nota
     sin reiniciar; dentro o fuera lo decide `point_in_polygon` (puro).
     Si no se sabe (la zona no está en el mapa activo, sin pose, sin red
     la primera vez), se hace como sin zona y se avisa en el log: es lo
     que hacía antes. Se hereda de la flota como `after_s`, salvo con otra
     action (lleva a otro sitio), como los `params`. En el Configurador,
     un desplegable con las zonas del catálogo del site de la flota y el
     texto del panel explica las dos condiciones (tiempo y zona); Guía al
     día. Comprobado contra los dos robots (solo lectura): los dos dentro
     de «zona de espera».

Probado por el usuario con el FM nuevo (#35): con la zona puesta, los
robots que ya están dentro no salen; una order lanzada mientras un robot iba
a la espera la cancela y la hace él, y 30 s después de terminarla vuelve a
la espera. Con los dos fuera a la vez, `Ir a zona de espera MIRUR` los
reparte entre los dos puntos de espera. #35 hecho.

## Pendiente (a 2026-10-29)

Lista única de lo abierto. **Los números (`#21`) son identificadores, no orden:** se
citan en decisiones anteriores ("punto 21"), así que no se renumeran; lo
cerrado se quita (su historia está en la entrada de su fecha). Cerrados el
2026-10-14: #4 (GUIDs comunes), #17 (descartar robot sin la mission), #20
(mapa activo: ya es `suberri_mirur`), #21 (un solo FM por broker, decisión 107)
y #16 (configurador completo: v3, decisión 108).
Cerrados el 2026-10-29: #30 (resuscribir al reconectar, decisión 120), #32
(rechazos acotados, decisión 121), #6 (`--validate`, decisión 122) y #23
(zonas del mapa desde la pestaña Crear, decisión 124). Hechos el mismo día
sin número: subir un mapa, crear un site, site de la flota, missions
atadas a su mapa, capturar un mapa de un robot y retirar mapas
(decisiones 126–133). Y #34 (crear grupos de missions, decisión 134).

**En curso / siguiente** (al cerrar el 2026-10-29; orden decidido por el usuario)

1. **2026-10-30: pruebas con robot.** La vuelta a la espera (#35, hecha
   el 2026-10-30) y el bloque «Probar con robot» de abajo. Antes, reiniciar el FM real (coge
   la retirada de mapas, los grupos, los datos en `mirur-instalacion` y
   la vuelta a la espera). El Error de mir-2 ya está quitado
   (2026-10-30).
2. **Pestaña Robot:** #24, #26 y #27 (abajo, en su bloque).
3. **#36 Modo mantenimiento por robot** (de `docs/sugerencias_mejora.md`):
   sacarlo de las asignaciones sin quitarlo de `fleet.yaml`; sigue
   publicando telemetría. Falta decidir qué pasa con las orders dirigidas
   a ese robot, la auto-carga, la vuelta a la espera y una order en curso
   (entrar en mantenimiento no debe abortarla).

Los avisos por tiempo anómalo (#5) esperan a las missions reales (#1): hoy
las del FM son placeholders y no hay tiempos que medir.

- **Estado al cerrar:** el FM real hay que reiniciarlo para que coja la
    retirada de mapas (decisión 133). Los dos robots están en el mapa
    `suberri` (de otros proyectos): con el reinicio, el FM ya rechaza las
    actions que van a positions fijas de `suberri_mirur` (decisión 131), y
    si se elige `suberri_mirur` como site de la flota, ninguno recibe
    orders hasta cambiar su mapa. mir-2 quedó en Error (10101) por una
    mission de `suberri_mirur` lanzada en `suberri`; quitado el
    2026-10-30.
- **Sin commitear en el catálogo** (`mirur-instalacion/catalog/`, del usuario): `mapa-pruebas`
    (`…-map0000000-01-0001-002`, subido con origen (12,5; 12,5) en vez del
    de `suberri_mirur`, (−13,825; −3,275), y ya desplegado en los dos
    robots), la zona «zona de carga» (`…-zone000000-01-0001-001`) y los
    borradores `cuadro_electrico` (mission y position).
- **#33** **Copiar un mapa del catálogo** con imagen, origen, positions y
    zonas (GUIDs nuevos de ese mapa). Primer uso: rehacer `mapa-pruebas`
    como copia de `suberri_mirur` (antes, «Borrar» el actual en Crear, que
    deja libre su número). Decidir qué pasa con las missions de positions
    fijas (decisión 131): copiarlas apuntando a las positions copiadas o
    pasarlas a recibir la position como input.


**Fiabilidad** (revisión del 2026-10-29, ver `docs/sugerencias_mejora.md`)

- **#31** **Registro de `fleet/order` aceptadas** (antes de #2). Hoy solo se
    recuerda la última (`Dispatcher.last_fleet_order`): con A → B → A, A
    se ejecuta dos veces. Guardar las últimas N `(orderId, orderUpdateId)`
    aceptadas con su `assignedSerial` y, ante un reenvío, **repetir el
    `order_response` ASSIGNED** en vez de callar. Cambia la decisión 19
    (idempotencia = silencio): acordarlo con quien hace el emisor de
    `fleet/order`. Los rechazos no se guardan (la decisión 20 sigue: un
    reintento tras `REJECTED` se acepta). Solo en memoria; la persistencia
    sigue fuera de alcance.

**Aguas arriba (a cargo de otro compañero, sobre este FM)**

- **#1** **Missions definitivas** ("coge 5 contactores y llévalos a la balda 3"):
   crearlas (pestaña Crear → web del MiR → Capturar → Sincronización →
   Configurador) con sus `number_inputs` / `position_inputs` /
   `required_inputs`.
- **#2** **Emisor de `fleet/order` en el sistema aguas arriba**: topic
   `vda5050/v3/imperial_fleet/fleet/order`, header
   `imperial_fleet`/`fleet`. Los números pueden ir como número o texto.

**Configurador y sincronización, más adelante**

- **#19** **Sincronización, siguientes versiones:** recapturar desde la UI una
    position que no se creó en la pestaña Crear (p. ej. un marcador
    recalibrado en un robot; hoy, con `POST /api/create/<serial>/capture` o
    `scripts/catalog_capture.py`); sustituir positions y mapas distintos;
    desplegar mapas a un robot cuyo site activo es otro (no probado).
    Hecho el 2026-10-23: adoptar missions hechas en la web (decisión 116) y
    sustituir missions distintas (decisión 117).
- **#28** Terminar `coger` (decisiones 116–118). Hecho: adoptada y desplegada
    en mir-1, y la copia `181aad0a-…` borrada en su web. Falta: cuando
    MESA_TKNIKA esté colocada, recapturarla de mir-1 y desplegar en mir-2
    (MESA_TKNIKA nueva + sustituir `coger`) y comprobar allí el atraque
    (offset calibrado con mir-1).
**Funcionalidad nueva**

- **#5** **Avisos por tiempo anómalo de mission** (idea anotada el 2026-10-11).
   Objetivo: avisar a los operarios cuando una mission tarda más de lo
   normal. Ejemplos: "posible obstáculo / camino bloqueado" si la fase de
   "ir a recoger" se alarga, o "problema en almacén" si el robot pasa allí
   más tiempo del previsto. El MiR solo avisa cuando entra en error; un
   robot esquivando un palé o esperando en el almacén sigue en `Executing`
   sin que nadie se entere. Primero los avisos salen en la interfaz; el
   sonido viene después.
   - **Sonido, más adelante.** El MiR no tiene endpoint REST para reproducir
     un sonido (`/sounds` solo lista y sube audios). Encolar una mission de
     sonido no sirve: la `mission_queue` es secuencial y no interrumpe la
     mission en curso. Opciones: (a) altavoz externo (ESP32/Raspberry)
     suscrito al `state` VDA, sin tocar FM ni missions; (b) bloque en cada
     mission que reproduce un sonido según un registro PLC escrito por el
     FM (mismo mecanismo que `n_piezas_pedidas`). Las dos se basan en el
     mismo aviso del `state`, así que hacerlo primero en la UI no obliga a
     rehacer nada.
   - **Hecho el 2026-10-29: paso 1 y herramienta del paso 2** (decisión 123).
     Queda: poner `expected_s` a las actions reales (a mano al principio;
     con `scripts/mission_durations.py` cuando tengan historia), el paso 3
     (por fases) y el sonido.
   - **Paso 1 — tiempo total por `actionType`.** Umbral en `fleet.yaml`
     (p. ej. `expected_s: 180`). Si se supera: entrada en `state.errors[]`
     con `errorLevel: WARNING` (en v3, "el robot puede seguir") y
     `errorReferences` → `orderId`; la UI la destaca. Es genérico: no
     necesita saber nada del MiR.
   - **Paso 2 — medir antes de fijar umbrales.** Registrar la duración de
     cada ejecución (y de cada fase si se puede) y sacar el tiempo "normal"
     de datos reales (p. ej. percentil 90), no a ojo.
   - **Paso 3 — por fases.** El FM hoy solo ve la mission entera
     (RUNNING/FINISHED). Para saber la fase hay dos fuentes:
     `GET /mission_queue/<id>/actions` (si no recordamos mal, da cada
     action con estado y horas de inicio/fin: más robusto) o asociar fases
     a patrones de `mission_text` (frágil: depende del idioma y de la versión
     del software). El driver MiR traduciría eso a un nombre de fase genérico
     en `Telemetry` y el core compararía con el umbral de cada fase, para
     no romper la regla de §4 (el core no sabe nada de MiR).
   - **Camino bloqueado = sin progreso, no solo tiempo.** Si está en una
     fase de movimiento y la posición apenas cambia durante N s, es casi
     seguro un obstáculo. Avisa antes y da menos falsos positivos. La
     posición ya llega a 1 Hz.
   - **Cuidado con:** no contar el tiempo en `Pending` (p. ej. encolada
     detrás de una carga) ni en pausa (`state_id 4`); el aviso se retira
     solo al avanzar la fase o terminar la mission, sin repetirse en cada
     tick; un WARNING no es un FAILED.
   - **Hecho con robot (2026-10-29, solo lectura).** (a) Fases: la pestaña
     Robot ya lee `GET /mission_queue/<id>/actions` (decisión 113) y cada
     paso trae tipo (`action_type`), estado y horas de inicio y fin: el
     paso 3 es viable sin depender de `mission_text`. (b) Duraciones: las
     300 últimas entradas terminadas de cada robot (junio–octubre; mir-1
     tiene 1.559 en la cola, mir-2 588) dan, para las missions con
     historia, distribuciones estrechas con colas largas, justo lo que el
     aviso debe detectar: `Apertura Puerta H2DX` (mir-2, 42 Done) mediana
     50 s, p90 61 s, máx. 71 s; `Move` (mir-2, 82) mediana 24 s, p90 50 s,
     pero máx. 447 s; `Cambiar a VGC10` (mir-2, 46) p90 153 s y máx.
     2.295 s. La espera en `Pending` es casi siempre 0. **Consecuencias:**
     el paso 2 no necesita que el FM registre nada, porque la historia del
     propio MiR ya da las duraciones (un script puede proponer el
     `expected_s` de cada action a partir de ella); las missions del FM
     (`coger`, `Ir a zona de espera MIRUR`, las de prueba) tienen aún 1–3
     ejecuciones, así que su primer umbral se pone a mano y se ajusta
     cuando haya historia. Las de carga (20–30 min, muy variables) quedan
     fuera del aviso.

**Probar con robot (hecho, solo con tests o con simulación)**

- **#7** **Asignador, siguiente candidato** (decisión 89): una action cuya
   mission solo tenga el robot con menos batería.
- **#8** `required_inputs` (parámetro de texto): no hay mission que lo use.
- **#9** Header ≠ topic → `REJECTED VALIDATION_FAILURE`.
- **#10** Order update (`orderUpdateId > 0`) sobre una order terminada.
- **#18** **Comprobar con mir-1 el atraque en los marcadores** `H2D1-VL`,
    `H2D2-VL` y `toolchanger-vl`: sus docking offsets se calibraron con
    mir-2 (la pestaña lo avisa). Y al revés con `MESA_TKNIKA` si algún día
    se lleva a mir-2.
- **#22** **Configurador y pestañas de administración con el FM real:** cambiar
    en caliente las actions de un MiR y ver que reindexa y que el
    asignador lo tiene en cuenta; desplegar con el robot ocupado (solo
    crear, decisión 103); quitar un sobrante de un robot y retirar algo de
    prueba de la flota (decisiones 105–106); alta de un MiR real y
    «Reiniciar el FM ahora» con los robots (decisión 108). Ya probado por el usuario:
    crear, abrir en la web del MiR, capturar y borrar (decisiones 100–104).

- **#29** **Probar con un marcador real** crear un VL-marker desde la
    pestaña Crear (decisión 119): crear delante del marcador, re-detectarlo
    en la web, capturar y desplegar en el otro robot. Cuando haya un
    marcador nuevo que poner (o con uno de prueba que luego se borra).

**Pestaña Robot** (decisiones 110–112; orden acordado el 2026-10-23)

- **#24** Verificar el formato real de `user_prompt` (decisión 111): lanzar
    una mission con pregunta al operario y mirar `/status`. (La velocidad de
    giro ya está verificada: grados/s.)
- **#26** Sección Fleet arriba, con mapa de todos los robots.
- **#27** Cámara: decidir la fuente (el MiR no da imagen por REST).

**Entorno**

- **#3** **Acceso a la web desde otros equipos**: comprobar si WSL está en modo
   `mirrored` o NAT (en NAT: `netsh portproxy` + regla de firewall).

**Menor**

- **#12** `prueba_parametros_v2` desde una tablet (deslizador).
- **#13** `powerSupply.charging` desde `/status`: hoy se aproxima con "mission de
   carga del FM en marcha" (decisión 35, coincidió en la prueba real). Solo
   haría falta si el robot carga sin que lo mande el FM.
- **#14** Retained antiguo en el Mosquitto local: `vda5050/v3/MiR/fleet/connection`
    (de antes de la decisión 30; el código actual ya no lo publica). Se borra
    con `mosquitto_pub -r -n -t vda5050/v3/MiR/fleet/connection`.
- **#15** Nota para leer logs: en WSL2 el reloj de pared salta a veces unos
    segundos al resincronizarse con Windows (visto el 2026-10-12: hueco de
    4 s en las marcas de tiempo con el `headerId` continuo). El bucle usa
    `time.monotonic()` y no se ve afectado; el `timestamp` VDA sí refleja
    el salto, como debe (es hora UTC).
