# Plan: cola de tareas en el FM (`fleet/order` con late binding)

Estado (2026-10-30): **fases 1 y 2 hechas**. Fase 1: cola, contrato MQTT,
cancelar, caducidad y tamaño máximo, también por la API REST; probada con
los MiR. Fase 2: SQLite y recuperación tras reinicio (también de las tareas
en curso, que el plan dejaba para la fase 4); probadas las dos con los
MiR (§8). Faltan las fases 3 y 4. Decisiones 154–162 de
`docs/avance.md`.

## 1. Qué cambia

Antes, `fleet/order` asignaba en el momento de llegar. Si no había robot
libre, respondía `REJECTED` con `NO_MOBILE_ROBOT_AVAILABLE` y el sistema de
arriba tenía que reintentar.

Esto cambia dos límites que el brief (`CLAUDE.md` §1) dejaba fuera de la v1
a propósito: "Cola interna de órdenes: si ningún robot puede aceptarla, se
rechaza" y "Persistencia: el estado vive en RAM y en el robot".

Con la cola:

- Una order válida que **algún** robot de la flota podría hacer se acepta y
  se guarda (`QUEUED`), aunque ahora mismo estén todos ocupados.
- En cada tick, si hay un robot libre, se le da la primera tarea pendiente
  que pueda hacer. Así la tarea va al primero que termina (late binding).
- Cada robot sigue teniendo **como mucho una** mission del FM. No se usa la
  `mission_queue` del MiR para guardar trabajo pendiente.
- `<serial>/order` (a un robot concreto) no cambia: o se acepta o se
  rechaza, sin cola, aunque haya tareas esperando (D7).

Se sigue rechazando al momento (`REJECTED`) lo que no va a poder hacerse
nunca: order mal formada, parámetro obligatorio ausente, o una order que
ningún robot de la flota puede traducir (no tiene la action habilitada, le
falta la mission…). Encolar eso sería dejarlo esperando para siempre. La
comprobación traduce la order en cada robot que tiene la action, sin red.

## 2. Ciclo de vida de una tarea

```
              ┌──── cancelar / caducar ────► CANCELLED | EXPIRED
              │
 fleet/order ─► QUEUED ──► ASSIGNING ──► ASSIGNED ──► FINISHED | FAILED
                  ▲            │
                  │            └── el robot no la acepta al lanzarla ──► FAILED
                  └── tras reiniciar el FM: el robot no la recibió
```

- **QUEUED**: guardada y esperando robot.
- **ASSIGNING**: elegido el robot, guardada *antes* del POST (decisión 161).
  En marcha normal dura lo que tarda el POST; solo se ve tras una caída del
  FM justo ahí (§5.4).
- **ASSIGNED**: el robot la ha aceptado. Se guarda `serial` y `job_id`.
- **FINISHED / FAILED**: lo que diga el tracker de la order (`fm/orders.py`).
  Se sigue en cada tick; no se publica (el resultado ya está en el `state`
  del robot).
- **CANCELLED**: cancelada con `fleet/instantActions` (D3).
- **EXPIRED**: ha superado `queue.max_wait_s` esperando robot (D4).

Si el robot no acepta la mission al lanzarla (red, 4xx), la tarea **no**
vuelve a la cola ni se prueba en otro robot (decisión 157): tras un timeout
el MiR pudo encolarla igual, y acabarían dos robots con la misma order. Es
la misma regla que ya tenía `fleet/order` sin cola. Si falla en el mismo
momento de llegar (el emisor aún no sabía nada de ella), la respuesta es
`REJECTED` como antes y se puede reintentar con el mismo `orderId`; si ya
estaba en la cola, `FAILED`.


## 3. Contrato MQTT

`fleet/order_response` pasa a tener **varios mensajes por order**:

| `status` | Cuándo | Campos extra |
|---|---|---|
| `QUEUED` | al llegar, si no hay robot libre | `queuePosition`, `description` (por qué espera) |
| `ASSIGNED` | al lanzarla en un robot (al momento o más tarde) | `assignedSerial`, `description` |
| `REJECTED` | no se acepta (inválida, imposible, cola llena) | `errorType`, `errorDescription` |
| `FAILED` | estaba en la cola y el robot no la aceptó al lanzarla; o, tras reiniciar el FM, no se pudo retomar | `errorType`, `errorDescription` o `description` |
| `CANCELLED` / `EXPIRED` | salió de la cola sin ejecutarse | `description` |
| `CANCEL_FAILED` | `cancelOrder` de algo que no está en la cola ni en curso | `errorType`, `errorDescription` |

Si llega una order y hay un robot libre, sale `ASSIGNED` directamente, sin
pasar por `QUEUED`. Para quien solo mira `ASSIGNED`/`REJECTED` no cambia
nada salvo que, con los robots ocupados, ahora recibe `QUEUED` y más tarde
`ASSIGNED`.

Topic retenido **`fleet/queue`** (FM → bus, QoS 1): las tareas que esperan,
en orden, con su `status` (`QUEUED`, o `ASSIGNING` + `serial` si está en
duda tras un reinicio, §5.4). Se publica cuando cambia, y en el primer tick
aunque esté vacía.

```json
{ "headerId": 3, "timestamp": "…", "version": "3.0.0",
  "manufacturer": "imperial_fleet", "serialNumber": "fleet",
  "tasks": [ { "orderId": "b3", "orderUpdateId": 0, "actionType": "coger", "status": "QUEUED",
               "receivedAt": "2026-10-30T11:01:21.918Z",
               "waiting": "ningún robot puede atender la order: sim-1: ocupado; sim-2: ocupado" } ] }
```

Cancelar (D3): `fleet/instantActions` con `cancelOrder` y el parámetro
`orderId`. Si la tarea espera, sale de la cola; si está en un robot, es el
`cancelOrder` de ese robot (`driver.cancel`). La respuesta va a
`order_response` (`CANCELLED` o `CANCEL_FAILED`), porque `fleet` no tiene
`state`. Otras instantActions en `fleet/` se ignoran con un aviso en el log.

```json
{ "headerId": 1, "timestamp": "…", "version": "3.0.0",
  "manufacturer": "imperial_fleet", "serialNumber": "fleet",
  "actions": [ { "actionType": "cancelOrder", "actionId": "c-1", "blockingType": "HARD",
                 "actionParameters": [ { "key": "orderId", "value": "b5" } ] } ] }
```

Duplicados y actualizaciones (mismas reglas de `orderUpdateId` que §6.1.4):

- Mismo `orderId` y `orderUpdateId` que una tarea en cola, en curso o
  terminada bien → se ignora (sin respuesta). Si terminó `FAILED`,
  `CANCELLED` o `EXPIRED`, se puede volver a mandar.
- `orderUpdateId` mayor de una que espera → la sustituye **en su mismo
  puesto** y se responde `QUEUED` otra vez. De una en curso → `REJECTED`
  (v1 no admite ampliar una order en curso). Menor → `REJECTED
  OUTDATED_ORDER_UPDATE`.

## 4. Asignación

En cada tick del bucle principal, después de leer la telemetría
(`Dispatcher.tick()` en `run_fm.py`, tras `poll_all`), y al llegar cada
`fleet/order`:

```
para cada tarea QUEUED, por orden de llegada:
    result = assign(tarea.actionType, snapshots, cfg)     # el de siempre
    si hay candidato y puede traducirla → execute → ASSIGNED
    si no → se queda en QUEUED (con el motivo) y se mira la siguiente
```

- Se recorren **todas** las tareas, no solo la primera. Si la primera solo
  la puede hacer mir-1 (ocupado), la segunda puede ir a mir-2.
- Una order que llega con un robot libre **no se cuela**: entra por detrás
  y el robot se lo lleva la primera tarea que pueda hacerlo.
- `assign()` no cambia: sigue filtrando por action habilitada, batería,
  ocupación y site. Lo que cambia es quién lo llama y cuándo.
- "Libre" es lo mismo que antes. La vuelta a la espera (`idle_return`) no
  ocupa al robot: el FM la cancela al lanzar la order (`fm/idle.py`). La
  auto-carga sí lo ocupa (D6, pendiente).
- El motivo de espera va al log solo cuando cambia (se evalúa en cada tick).

## 5. Dónde se guarda el dato

### 5.1. Opciones

| Opción | A favor | En contra |
|---|---|---|
| Solo RAM | lo más simple; ya es como funciona todo el FM | al reiniciar se pierden las tareas no empezadas, y nadie más las tiene |
| Fichero JSON | legible a mano; mismo patrón que `fleet.yaml` (temporal + `os.replace`) | se reescribe entero en cada cambio; el histórico lo hace crecer; consultas a mano |
| **SQLite** | viene con Python (`sqlite3`), sin servicio extra; un fichero; transacciones atómicas que aguantan un corte; consultas SQL | algo más de código que el JSON; el fichero no se lee con un editor de texto |
| Servidor (PostgreSQL, Redis) | varios procesos o máquinas a la vez | un servicio más que instalar y mantener para un único FM; no hace falta |

### 5.2. Decidido: SQLite detrás de `TaskStore` (decisiones 155 y 160)

`fm/tasks.py` define la interfaz `TaskStore` (`add`, `save`, `remove`,
`alive`, `find`), igual que los robots van detrás de `RobotDriver`. El
`Dispatcher` cambia los campos de la `Task` y llama a `save()`; no sabe
dónde se guarda. `MemoryTaskStore` (RAM, las últimas 500 terminadas) queda
para los tests; el FM usa `SqliteTaskStore`. Pasar a Redis u otro sería otra
clase, sin tocar la asignación.

Un fichero por `fleet.yaml`: `<FM_DATA>/<nombre del fleet.yaml>.sqlite`.

- Por defecto `data/fleet.sqlite` dentro del FM (`data/` en `.gitignore`,
  con su README). `fleet-sim.yaml` usa `data/fleet-sim.sqlite`: una flota
  simulada no comparte cola con la real.
- Otra carpeta: `FM_DATA` en `.env` o `run_fm.py --data`.
- No va en `mirur-instalacion`. Ese repo guarda la configuración de la
  instalación, no el estado de funcionamiento.
- En compose, `./data` montado en `fleet` y `fleet-sim`.

Motivos: el dato tiene que sobrevivir a un reinicio (el sistema de arriba ya
recibió `QUEUED` y no lo va a reenviar). Cada cambio de estado es una
escritura pequeña que tiene que ser atómica. Y el histórico se queda en la
misma tabla y se puede consultar. Como material de clase, además, es una
base de datos real sin montar ningún servidor.

### 5.3. Tabla

```sql
CREATE TABLE tasks (
    id          INTEGER PRIMARY KEY,           -- orden de llegada
    order_id    TEXT    NOT NULL,
    update_id   INTEGER NOT NULL,
    action_type TEXT    NOT NULL,
    payload     TEXT    NOT NULL,              -- la order VDA tal como llegó, en JSON
    status      TEXT    NOT NULL,              -- QUEUED, ASSIGNING, ASSIGNED, FINISHED…
    announced   INTEGER NOT NULL DEFAULT 0,    -- ya se respondió QUEUED al emisor
    serial      TEXT,                          -- robot, desde ASSIGNING
    job_id      TEXT,                          -- id del driver (mission_queue del MiR)
    received_at REAL    NOT NULL,              -- epoch UTC
    assigned_at REAL,
    finished_at REAL,
    result      TEXT    NOT NULL DEFAULT '',   -- motivo de FAILED / CANCELLED / EXPIRED
    waiting     TEXT    NOT NULL DEFAULT ''    -- por qué no se ha asignado aún
);
```

Se guarda la order entera (`payload`) para poder traducirla y lanzarla tal
cual después de un reinicio. `id` propio en vez de la clave `(order_id,
update_id)` que proponía el plan: una order FAILED se puede volver a mandar
con el mismo id y son dos filas. Una actualización de una que espera
reescribe su fila, así que conserva el sitio. Sin columna de prioridad (D2);
se añade si algún día hace falta.

### 5.4. Cómo se gestiona

- **Un solo escritor**: el hilo principal (el `Dispatcher`). Ya es quien
  procesa todo lo que llega por MQTT. La web no lee de aquí: lee el espejo
  MQTT (`fleet/queue`, decisión 145) y cancela publicando un `cancelOrder`.
- **Primero se guarda, luego se responde**: un `commit` por cambio, con el
  diario por defecto de SQLite (no WAL). Lo que el emisor vio aceptado está
  en disco aunque se vaya la luz.
- **Primero `ASSIGNING`, luego el POST** (decisión 161). La tarea lleva una
  etiqueta, `<orderId>@<hora de llegada>`, que el driver deja en el robot (en
  el MiR, en el `description` de la entrada de la `mission_queue`).
- **Al arrancar** (`Dispatcher.recover()`, con la telemetría ya leída):
  - `QUEUED` siguen en la cola, y se repite su `QUEUED` (con el puesto): el
    historial de la API REST vive en RAM y el emisor puede haberlo perdido.
  - `ASSIGNED`: el driver la adopta (`adopt_job`: deja de parecer trabajo
    ajeno), el tracker vuelve a seguirla y se repite el `ASSIGNED`.
    Adelantado de la fase 4: sin
    esto, una tarea que el robot sigue haciendo se marcaba FAILED. Sin
    `adopt_job` (driver `sim`) → FAILED con el motivo.
  - `ASSIGNING`: se busca la etiqueta en el robot (`find_job`, las 20
    entradas más nuevas de su `mission_queue`). La tiene → `ASSIGNED` (y se
    responde `ASSIGNED`, que el emisor quizá no recibió). Seguro que no →
    vuelve a la cola en su sitio. No se puede saber (sin red, driver sin
    `find_job`) → **se queda en duda** (D9, decisión 162): sale en
    `fleet/queue` con `status: ASSIGNING` y su `serial`, se vuelve a buscar
    cada 30 s, y `cancelOrder` la descarta. Nunca se relanza a ciegas.
    Por etiqueta y no por hora: los relojes del PC y del robot no tienen por
    qué coincidir.
  - Un robot de la flota que no corre en este FM (`--robot`): sus tareas no
    se tocan.
- **Histórico**: las tareas terminadas se quedan en la tabla y se purgan las
  de más de `queue.keep_days` días al arrancar (D8: 30; vacío = nunca). Ese
  mismo histórico serviría para calcular `expected_s` con datos reales.
- **En RAM**: una copia de las tareas vivas, para no consultar el fichero en
  cada tick. El fichero manda: la copia se rehace desde él al arrancar.

## 6. Web del FM (fase 3)

Fase 1: el bloque `fleet/order` y los eventos ya enseñan `QUEUED` (con su
puesto), `CANCELLED`, `EXPIRED` y `FAILED`, y las orders enviadas que esperan
tienen «Quitar de la cola». API REST (decisión 158): `GET /api/queue` y
`POST /api/orders/<id>/cancel`; la pasarela de voz para con esta última
(decisión 159).

Fase 3, en la pestaña de operación, un bloque "Cola" con:

- Las tareas pendientes en orden (`fleet/queue`), con tiempo de espera y
  por qué no se asignan (lo que dice `assign()` de cada robot: "ocupado",
  "batería 17% < 20%"…).
- Un botón de cancelar.
- Las tareas en duda tras un reinicio (D9), con relanzar / descartar.
  Descartar ya se puede (`cancelOrder`, decisión 162); relanzar, no.

## 7. Decisiones (acordadas el 2026-10-30)

- **D1. Persistencia**: SQLite detrás de `TaskStore` (§5.2).
- **D2. Orden**: por llegada. La prioridad iría en un campo propio (VDA 5050
  no la trae) y obliga a decidir qué pasa con las que nunca salen; se
  añade cuando un caso real la pida.
- **D3. Cancelar desde arriba**: `fleet/instantActions` + `cancelOrder` con
  el parámetro `orderId` (§3). Hecho.
- **D4. Caducidad**: `queue.max_wait_s` global en `fleet.yaml`; vacío =
  espera para siempre. Hecho. Por order, si hace falta más adelante.
- **D5. Tamaño máximo**: `queue.max_size` (50 por defecto); la siguiente se
  rechaza con `QUEUE_FULL`. Hecho.
- **D6. Robot cargando**: una tarea podrá sacarlo del dock por encima de
  `battery_resume` (p. ej. 50 %). Pendiente.
- **D7. Order dirigida con tareas esperando**: se acepta igual. Es lo que se
  usa para pruebas y casos puntuales. Nada que cambiar.
- **D8. Histórico**: `queue.keep_days`, 30 días. Hecho.
- **D9. Tarea en duda al arrancar**: bloqueada y reintentando; sale en
  `fleet/queue` y se descarta con `cancelOrder`. Hecho; el aviso y
  "relanzar" en la web, fase 3.

## 8. Fases

1. **Hecha (2026-10-30). Cola en RAM** detrás de `fleet/order`, con el
   contrato de §3, la asignación de §4, cancelar (D3), caducidad (D4) y
   tamaño máximo (D5). `tests/test_queue.py` y prueba con la flota simulada
   (dos robots, seis orders, cancelar una, cola llena; la tercera y la
   cuarta van a cada robot según termina).
2. **Hecha (2026-10-30). SQLite**: `SqliteTaskStore`, `ASSIGNING` con la
   etiqueta en el robot, recuperación de `QUEUED`, `ASSIGNING` (D9) y
   `ASSIGNED` al arrancar, duplicados tras reinicio y purga del histórico
   (D8). `tests/test_tasks.py`, `tests/test_queue_restart.py` y prueba con
   la flota simulada y `kill -9`.
3. **Web**: bloque Cola, cancelar y tareas en duda (relanzar).
4. Histórico → `expected_s` y D6 (sacar del dock).
5. **Hecha (2026-10-30). Prueba con los dos MiR** (mission `prueba-cola`):
   el MiR guarda el `description` del POST y `find_job` lo encuentra;
   reinicio del FM con dos tareas en curso y dos en cola, sin ninguna
   repetida; y el reparto entre los dos robots.
