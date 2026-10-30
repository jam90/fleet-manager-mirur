# Plan: cola de tareas en el FM (`fleet/order` con late binding)

Estado: **fase 1 hecha (2026-10-30)**: cola en RAM, contrato MQTT,
cancelar, caducidad y tamaño máximo, también por la API REST; probada con
la flota simulada. Faltan las fases 2–5 (§8). Decisiones 154–159 de
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
 fleet/order ─► QUEUED ──► ASSIGNED ──► FINISHED | FAILED
                  │
                  └── el robot no la acepta al lanzarla ──► FAILED
```

- **QUEUED**: guardada y esperando robot.
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

**`ASSIGNING`** (elegido el robot, antes del POST) se deja para la fase 2:
solo sirve para recuperar una caída del FM entre el POST y la respuesta, y
con la cola en RAM no hay nada que recuperar.

## 3. Contrato MQTT

`fleet/order_response` pasa a tener **varios mensajes por order**:

| `status` | Cuándo | Campos extra |
|---|---|---|
| `QUEUED` | al llegar, si no hay robot libre | `queuePosition`, `description` (por qué espera) |
| `ASSIGNED` | al lanzarla en un robot (al momento o más tarde) | `assignedSerial`, `description` |
| `REJECTED` | no se acepta (inválida, imposible, cola llena) | `errorType`, `errorDescription` |
| `FAILED` | estaba en la cola y el robot no la aceptó al lanzarla | `errorType`, `errorDescription` |
| `CANCELLED` / `EXPIRED` | salió de la cola sin ejecutarse | `description` |
| `CANCEL_FAILED` | `cancelOrder` de algo que no está en la cola ni en curso | `errorType`, `errorDescription` |

Si llega una order y hay un robot libre, sale `ASSIGNED` directamente, sin
pasar por `QUEUED`. Para quien solo mira `ASSIGNED`/`REJECTED` no cambia
nada salvo que, con los robots ocupados, ahora recibe `QUEUED` y más tarde
`ASSIGNED`.

Topic retenido **`fleet/queue`** (FM → bus, QoS 1): las tareas que esperan,
en orden. Se publica cuando cambia, y en el primer tick aunque esté vacía
(borra la de un FM anterior: la cola en RAM no sobrevive al reinicio).

```json
{ "headerId": 3, "timestamp": "…", "version": "3.0.0",
  "manufacturer": "imperial_fleet", "serialNumber": "fleet",
  "tasks": [ { "orderId": "b3", "orderUpdateId": 0, "actionType": "coger",
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

### 5.2. Decidido: SQLite detrás de `TaskStore` (decisión 155)

`fm/tasks.py` define la interfaz `TaskStore` (`add`, `save`, `remove`,
`alive`, `find`), igual que los robots van detrás de `RobotDriver`. El
`Dispatcher` cambia los campos de la `Task` y llama a `save()`; no sabe
dónde se guarda. Fase 1: `MemoryTaskStore` (RAM, guarda las últimas 500
terminadas para reconocer repetidas). Fase 2: `SqliteTaskStore`. Pasar a
Redis u otro sería otra clase, sin tocar la asignación.

Un único fichero, por ejemplo `data/fm.sqlite`:

- Por defecto dentro del FM, en `data/` (en `.gitignore`).
- Configurable con `FM_DATA` en `.env`, como `FM_CONFIG` y `FM_CATALOG`.
- No va en `mirur-instalacion`. Ese repo guarda la configuración de la
  instalación, no el estado de funcionamiento.

Motivos: el dato tiene que sobrevivir a un reinicio (el sistema de arriba ya
recibió `QUEUED` y no lo va a reenviar). Cada cambio de estado es una
escritura pequeña que tiene que ser atómica. Y el histórico se queda en la
misma tabla y se puede consultar. Como material de clase, además, es una
base de datos real sin montar ningún servidor.

### 5.3. Tabla (fase 2)

```sql
CREATE TABLE tasks (
    order_id     TEXT    NOT NULL,
    update_id    INTEGER NOT NULL,
    action_type  TEXT    NOT NULL,
    payload      TEXT    NOT NULL,   -- la order VDA completa, en JSON
    status       TEXT    NOT NULL,   -- QUEUED, ASSIGNING, ASSIGNED, FINISHED…
    serial       TEXT,               -- robot, desde ASSIGNING
    job_id       TEXT,               -- id del driver (mission_queue del MiR)
    received_at  REAL    NOT NULL,   -- epoch UTC
    assigned_at  REAL,
    finished_at  REAL,
    result       TEXT,               -- motivo de FAILED/CANCELLED/EXPIRED
    waiting      TEXT,               -- por qué no se ha asignado aún
    PRIMARY KEY (order_id, update_id)
);
CREATE INDEX tasks_status ON tasks(status);
```

Se guarda la order entera (`payload`) para poder traducirla y lanzarla tal
cual después de un reinicio. Sin columna de prioridad (D2: orden de
llegada); se añade si algún día hace falta.

### 5.4. Cómo se gestiona (fase 2)

- **Un solo escritor**: el hilo principal (el `Dispatcher`). Ya es quien
  procesa todo lo que llega por MQTT. La web no escribe directamente: pide
  "cancelar" por la misma vía que el configurador usa con el `Reloader`, y
  lee con su propia conexión. SQLite en modo WAL permite leer mientras otro
  escribe.
- **Primero se guarda, luego se responde**: `QUEUED` solo se publica cuando
  la fila ya está escrita. Si el FM cae, lo que el sistema de arriba vio
  aceptado está en disco.
- **Primero `ASSIGNING`, luego el POST**: así una caída entre el POST y la
  respuesta del robot no se convierte en una tarea olvidada ni duplicada.
- **Al arrancar**:
  - `QUEUED` siguen en la cola, sin más.
  - `ASSIGNING`: se mira la `mission_queue` del robot. Si hay una entrada
    de esa mission creada después de `assigned_at`, pasa a `ASSIGNED`; si
    no, vuelve a `QUEUED`. Si no se puede saber (robot sin red), **se queda
    bloqueada y se avisa en la web** para que alguien decida si se relanza
    o se descarta (D9): nunca se relanza a ciegas.
  - `ASSIGNED` sin terminar: hoy el FM olvida al reiniciar la order que
    tenía cada robot. Con el `job_id` guardado podría volver a seguirla
    (fase 4).
- **Histórico**: las tareas terminadas se quedan en la tabla y se purgan las
  de más de 30 días al arrancar (D8, configurable). Ese mismo histórico
  serviría para calcular `expected_s` con datos reales en vez de a mano.
- **En RAM**: una copia de las tareas vivas, para no consultar la base de
  datos en cada tick. La base de datos es la fuente de verdad. La copia en
  RAM se rehace desde ella al arrancar.

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
- **D8. Histórico**: 30 días, configurable (fase 2/4).
- **D9. Tarea en duda al arrancar**: bloqueada y aviso en la web (fase 2/3).

## 8. Fases

1. **Hecha (2026-10-30). Cola en RAM** detrás de `fleet/order`, con el
   contrato de §3, la asignación de §4, cancelar (D3), caducidad (D4) y
   tamaño máximo (D5). `tests/test_queue.py` y prueba con la flota simulada
   (dos robots, seis orders, cancelar una, cola llena; la tercera y la
   cuarta van a cada robot según termina).
2. **SQLite**: `SqliteTaskStore`, `ASSIGNING`, recuperación de `QUEUED` y
   `ASSIGNING` al arrancar (D9), duplicados tras reinicio. Tests de "matar
   el FM" en cada punto del ciclo de vida.
3. **Web**: bloque Cola, cancelar y tareas en duda.
4. **Recuperar `ASSIGNED`** tras reinicio, histórico (purga D8,
   `expected_s`) y D6 (sacar del dock).
5. Prueba con los dos MiR, avisando antes de lanzar nada.
