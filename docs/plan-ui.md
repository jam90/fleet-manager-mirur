# Plan: interfaz web del Fleet Manager

Objetivo: una página en el navegador (LAN de clase, sin login) que muestre
en tiempo real la batería y la misión de cada robot, permita pausar /
reanudar / cancelar, y enviar una `fleet/order` (con parámetros) para que el
FM elija el robot. Preparada para N robots y varias marcas: la UI no sabe
nada de MiR; todo lo que pinta sale del contrato VDA 5050 que el FM ya
publica.

Estado: **implementado y probado con robots el 2026-09-18** (ver `docs/avance.md`).

Decisiones tomadas con el usuario (2026-09-18): la UI la sirve el propio
FM (no se toca Mosquitto); se envían `fleet/order` con parámetros (no
orders dirigidas); acceso en LAN sin login; stack a criterio del
desarrollador pensando en N robots.

## 1. Idea central: la UI es un cliente VDA más

El FM ya publica todo lo que la UI necesita (`state`, `connection`,
`order_response`) y ya acepta todo lo que la UI quiere mandar (`fleet/order`,
`instantActions`). En vez de inventar una API paralela:

- **Salida**: cada mensaje que el FM publica por MQTT se **replica** a los
  navegadores conectados por WebSocket, tal cual (`{topic, payload}`). La UI
  ve exactamente lo que ve un sistema aguas arriba (Node-RED, un ERP…).
- **Entrada**: lo que la UI envía, el servidor web lo **publica en el
  broker** con el header VDA correcto (`fleet/order`,
  `<serial>/instantActions`). El FM lo recibe por su suscripción normal y
  lo procesa por el mismo camino que cualquier otro emisor. Aparece en
  `mosquitto_sub`, lo puede ver Node-RED, y no hay un segundo camino de
  entrada que mantener.

Ventajas: cero lógica duplicada, la UI sirve como demostración viva del
contrato MQTT (didáctico), y una marca nueva aparece en la UI sin tocarla
(publica `state` bajo su `manufacturer`, y el asignador la incluye).

Lo único que la UI necesita y que VDA no da hoy: **qué actionTypes existen y
qué parámetros llevan** (para pintar el formulario). Es justo lo que la
norma pone en `factsheet.agvActions`; se resuelve con un endpoint
`GET /api/fleet` que lo saca de `fleet.yaml` + driver (§2.3), y deja el
`factsheet` VDA a un paso.

## 2. Diseño

### 2.1 Backend (`fm/web.py`)

FastAPI + uvicorn (dos dependencias nuevas, estándar, bien documentadas para
clase) en un **hilo propio** con su event loop. Se comunica con el hilo
principal solo por colas, sin locks, siguiendo la decisión 14:

```
navegador ──WS──► uvicorn (hilo web) ──bus.publish()──► broker ──► FM (hilo principal)
navegador ◄──WS── uvicorn (hilo web) ◄── cola ◄── bus.publish_raw() (hilo principal)
```

- `bus.publish_raw()` gana un gancho `on_publish(topic, payload)`; el
  servidor web lo usa para encolar el mensaje hacia los clientes WS
  (`loop.call_soon_threadsafe`). Si no hay servidor web, no hay gancho.
- `bus.publish()` desde el hilo web: paho es thread-safe para `publish()`.
  El `HeaderCounter` es por topic y lo comparte con el hilo principal —
  única sección crítica: se protege con un `Lock` dentro de `HeaderCounter`
  (una línea).

Endpoints:

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/` | `index.html` |
| `GET` | `/api/fleet` | snapshot inicial: robots (serial, manufacturer, driver), actionTypes con sus parámetros y opciones, umbrales (`battery_min`, `battery_floor`), `fleet_manufacturer` |
| `WS` | `/ws` | al conectar: último `state`/`connection` de cada robot (retenido en memoria); después, cada publicación del FM `{topic, payload}` |
| `POST` | `/api/fleet/order` | `{actionType, params: {key: value}}` → publica `fleet/order`; responde `{orderId}`; el veredicto llega por WS (`order_response`) |
| `POST` | `/api/robots/{serial}/instant` | `{actionType}` (`startPause`/`stopPause`/`cancelOrder`) → publica `instantActions`; resultado por WS en `state.instantActionStates` |

`run_fm.py --web-port 8050` (0 = sin web, por defecto 8080). Escucha en
`0.0.0.0` (LAN). Sin autenticación (decisión del usuario; misma política que
la web del MiR).

### 2.2 Frontend (`fm/web/static/index.html`)

Un solo fichero HTML con **Vue 3 desde CDN, sin build** (ni npm ni
compilación): reactividad de serie para pintar una lista de N robots y
formularios con parámetros, y sigue siendo un fichero que se lee entero.
Si algún día crece (mapa, histórico), se migra a un proyecto Vue con build
sin reescribir la lógica.

Pantalla:

```
┌ Fleet Manager ─────────────────────── ● conectado al FM ─┐
│ ┌ mir-1 (MiR) ● ONLINE ┐ ┌ mir-2 (MiR) ● ONLINE ┐ ┌ sim-1 … ┐ │
│ │ ▓▓▓▓▓▓▓░░░ 74 % ⚡     │ │ ▓▓▓▓░░░░░░ 36 %       │ │         │ │
│ │ AUTOMATIC · driving   │ │ AUTOMATIC · paused    │ │         │ │
│ │ order fleet-3928/0    │ │ order —               │ │         │ │
│ │  coger  RUNNING       │ │ Moving to 'H2D2'…     │ │         │ │
│ │ [⏸ pausa][▶ play][✖ cancelar]                   │ │         │ │
│ │ errores: —            │ │ ⚠ ORDER_EXECUTION_FAILED … │       │ │
│ └───────────────────────┘ └───────────────────────┘ └─────────┘ │
│ ┌ Enviar a la flota ──────────────────────────────────────────┐ │
│ │ action [abrir_puerta ▾]  target_pos [H2D2 ▾]  [Enviar]      │ │
│ │ ✔ fleet-8d78 → mir-1 (asignado a mir-1 (74.9%))             │ │
│ │ ✖ fleet-2f9b REJECTED NO_MOBILE_ROBOT_AVAILABLE: mir-1: …   │ │
│ └─────────────────────────────────────────────────────────────┘ │
│ ┌ Eventos ────────────────────────────────────────────────────┐ │
│ │ 12:44:05 mir-1  coger WAITING → RUNNING                     │ │
│ └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

- Tarjeta por robot generada a partir de `state`: batería (`powerSupply`,
  con icono de carga), `operatingMode`/`driving`/`paused`, `orderId` +
  `actionStates`, `information[MISSION]` (texto del MiR), `errors[]`,
  `connection`. Colores: verde libre, azul ejecutando, amarillo pausado /
  cargando, rojo error / sin conexión.
- Botones ⏸ ▶ ✖ → `POST /api/robots/{serial}/instant`. Se deshabilitan
  según estado (no cancelar si no hay order, etc.) y muestran el resultado
  de `instantActionStates`.
- Panel "Enviar a la flota": select de actionType (unión de las actions de
  todos los robots, marcando cuántos la soportan) y un campo por parámetro
  (con desplegable si el driver da opciones). Al enviar, espera el
  `order_response` por WS y lo muestra.
- Lista de eventos: cambios de `actionStatus`, `order_response`, errores
  nuevos (últimos 50).
- Reconexión automática del WS y banner "FM desconectado".

### 2.3 Descripción de actions (`/api/fleet`)

Gancho **opcional** en el driver, `describe_actions() -> list[ActionInfo]`:

```python
@dataclass
class ParamInfo:  key: str; kind: str ("position" | "text"); required: bool; choices: list[str] | None
@dataclass
class ActionInfo: action_type: str; params: list[ParamInfo]
```

`MirDriver` lo saca de `MirRobotConfig.actions` (+ `positions` del índice
como `choices` de los `position_inputs`); `SimDriver` devuelve sus
actionTypes sin parámetros. Un driver sin el gancho → solo los nombres de
`RobotConfig.action_types`. Es la misma información que iría a
`factsheet.agvActions` (VDA §6.11): se deja preparado.

## 3. Fases

| # | Fase | Toca | Entrega |
|---|---|---|---|
| 1 | **Backend**: `fm/web.py` (FastAPI en hilo, WS espejo de publicaciones, `GET /api/fleet`, `POST` order/instant), gancho `on_publish` en `MqttBus`, `Lock` en `HeaderCounter`, `--web-port`. Tests con `TestClient` y bus falso. | web.py, mqtt_bus.py, header.py, run_fm.py, requirements.txt | `curl` y `websocat` contra el FM con `fleet-sim.yaml` |
| 2 | **UI de lectura**: `index.html` con tarjetas por robot (batería, estado, order, misión, errores, connection), eventos, reconexión. | web/static/index.html | ver los dos MiR y el sim en el navegador |
| 3 | **Controles**: pausa/play/cancelar y "Enviar a la flota" con parámetros; `describe_actions()` en MiR y sim. | index.html, adapters/base.py, mir/driver.py, sim/driver.py | prueba con robots: order desde la UI, pausa, cancelación |
| 4 | **Remate**: layout móvil, README ("Interfaz web"), bitácora, `CLAUDE.md`. | docs | — |

Estimación: 1 día. Fase 1 es la mitad.

## 4. Fuera de alcance (esta tanda)

- Orders dirigidas a un robot desde la UI (el usuario no las pidió; añadir
  un desplegable "robot" sería trivial sobre el mismo `POST`).
- Cambiar `battery_floor` en caliente, editar `fleet.yaml`, ver logs.
- Mapa con la posición de los robots (habría que servir la imagen del mapa
  del MiR; `mobileRobotPosition` ya viene en el `state`).
- Autenticación. Si algún día la UI sale de la LAN de clase, un token en
  cabecera para los `POST` es el mínimo.
- `factsheet` VDA retained (queda a un paso con `describe_actions()`).

## 5. Riesgos

- **Hilo web + hilo principal**: solo comparten `HeaderCounter` (con Lock)
  y el cliente paho (thread-safe para `publish`). Las lecturas del estado de
  los robots para `/api/fleet` son de configuración estática; el estado en
  vivo viaja por la cola de publicaciones, no se lee de `Robot` desde el hilo
  web.
- **Broker inaccesible desde la UI**: no aplica, la UI no habla con el broker.
- **Navegadores viejos en el taller**: Vue 3 CDN requiere ES2015+; cualquier
  Chrome/Firefox de los últimos años vale.
