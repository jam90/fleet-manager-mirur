# Plan: FM multimarca con drivers enchufables

Objetivo: que el core del FM (MQTT, VDA 5050, asignador, ciclo de vida de
orders) no sepa nada de MiR, y que añadir otra marca sea crear una carpeta
en `fm/adapters/` + una línea en un registro. Sin cambiar el contrato MQTT
actual ni el comportamiento con los dos MiR250.

Estado: **aprobado, pendiente de implementar**. Fecha: 2026-09-17.

Decisiones cerradas el 2026-09-17: manufacturer por robot (norma), segmento
`imperial_fleet` para los topics de flota, sin compatibilidad con el yaml
antiguo, driver `sim` incluido en esta tanda.

## 1. Diagnóstico

Ya es genérico: `fm/vda5050/*`, `fm/mqtt_bus.py`, `fm/assigner.py`,
`fm/fleet.py` (salvo un import de `pick_action`).

Acoplado a MiR:

| Fichero | Qué sabe de MiR |
|---|---|
| `fm/robot.py` | Crea `MirClient`; guarda índices nombre→GUID; `busy`/`available` leen `state_id` y `mission_queue_id`; `refresh_indices`. |
| `fm/orders.py` | `OrderTracker.poll(client)` hace `GET /mission_queue/{id}`; `QUEUE_TO_ACTION`; `ActiveOrder.queue_id/queue_state`; `check_new(status: MirStatus)` mira `state_id`. |
| `fm/adapters/mir.py` | Correcto que sea MiR, pero `StateOverlay` y `pick_action` son genéricos y viven aquí. |
| `fm/config.py` | `ActionConfig` = mission + inputs (modelo MiR); `mission_group`, `positions_allowlist`, `auto_charge.mission` son MiR. Env `MIR_HOST_*`, `MIR_AUTH_*`. |
| `run_fm.py` | Llama a `to_vda_state` y `refresh_indices`; el log del tick imprime `state_id`. |
| `fm/mqtt_bus.py` / `header.py` | Un único `manufacturer` global para todos los robots. |

Conclusión: la separación core / vda / adapter ya existe en espíritu; falta
formalizar la frontera y mover ~4 cosas al lado correcto.

## 2. Diseño

### 2.1 Interfaz del driver (`fm/adapters/base.py`)

Un `Protocol` (duck typing, sin herencia obligatoria) con lo mínimo que el
core necesita. Todo lo que devuelve son tipos **normalizados** definidos en
el mismo módulo; nunca tipos de la marca.

```python
class Telemetry:            # snapshot normalizado del robot, sin marca
    battery: float          # %
    pose: tuple[float, float, float] | None   # x, y, theta
    map_id: str | None
    driving: bool
    paused: bool
    charging: bool | None   # None = el driver no lo sabe
    operating_mode: str     # "AUTOMATIC" | "MANUAL" | ...
    emergency_stop: bool
    available: bool         # puede aceptar orders (no Manual/Error/E-stop)
    foreign_busy: bool      # ejecuta algo que no lanzó el FM (decisión 16/18)
    errors: list[Error]     # ya en formato VDA (MIR_<code>, etc.)
    information: list[Info]

class Job:                  # lo que el driver va a ejecutar para una action
    action: Action          # la action VDA
    label: str              # para el log ("mission 'coger'")
    payload: Any            # opaco: el driver guarda aquí lo que necesite

JobStatus = "WAITING" | "RUNNING" | "FINISHED" | "FAILED"   # = actionStatus VDA

class RobotDriver(Protocol):
    manufacturer: str                     # segmento del topic ("MiR")
    def connect(self) -> bool             # índices, handshake…; tolerante, se reintenta
    def poll(self) -> Telemetry | None    # None = inalcanzable; el driver guarda el motivo
    last_error: str | None
    def translate(self, action: Action) -> Job        # puro, sin red; lanza OrderRejected
    def execute(self, job: Job, priority: int = 0) -> str   # → job_id
    def job_status(self, job_id: str) -> JobStatus | None   # None = no se pudo consultar
    def cancel(self, job_id: str) -> None             # para instantActions/cancelOrder (H5)
    def charge_job(self) -> Job | None                # para auto-carga (H4); None = no soporta
    def extra_state(self, s: State) -> None           # gancho opcional: campos que el core no puede deducir
```

Decisiones de diseño:

- **`to_vda_state` pasa a ser del core** (`fm/vda5050/state.py` o
  `fm/state_builder.py`): construye `State` a partir de `Telemetry` +
  `StateOverlay`. El driver solo normaliza. Así una marca nueva no puede
  publicar un `state` mal formado por su cuenta.
- **`StateOverlay` y `pick_action` se mueven** a `fm/vda5050/` (overlay) y
  `fm/orders.py` (pick_action): son reglas VDA, no de MiR.
- **`OrderTracker` deja de tocar red**: guarda `job_id` y `job_status`, y en
  `poll()` pregunta `driver.job_status(job_id)`. `check_new` recibe
  `Telemetry` y usa `available`.
- **El driver es responsable de sus índices** (GUIDs, positions) y de
  reintentarlos: `Robot.indexed` desaparece; `connect()` devuelve bool y el
  core lo reintenta cada tick mientras sea False.
- **`Robot` queda como composición** `driver + OrderTracker + last_telemetry`,
  con `busy = tracker.busy or telemetry.foreign_busy`, `snapshot()` para el
  asignador, y `translate/execute` delegando. ~50 líneas.

### 2.2 Paquete MiR (`fm/adapters/mir/`)

```
fm/adapters/mir/
  __init__.py     # exporta MirDriver, parse_config
  client.py       # = fm/mir_client.py actual, sin cambios
  translate.py    # = from_vda_order + _mir_errors + estados → Telemetry (puro)
  driver.py       # MirDriver: implementa el Protocol; encapsula índices y mission_queue
  config.py       # MirRobotConfig: host, auth, actions{mission, position_inputs, required_inputs},
                  #   mission_group, positions_allowlist, charge_mission
```

`fm/mir_client.py` se deja como shim (`from fm.adapters.mir.client import *`)
una versión, porque `scripts/*.py` lo importan directamente.

### 2.3 Registro (`fm/adapters/__init__.py`)

```python
DRIVERS: dict[str, type[DriverFactory]] = {"mir": MirDriverFactory}
def make_driver(serial: str, raw_robot_cfg: dict, env: Mapping) -> RobotDriver
```

Cada factory recibe el dict crudo del robot en `fleet.yaml` + entorno y
devuelve el driver ya configurado. El core no valida claves de marca; cada
driver valida las suyas y falla al arrancar con un mensaje claro.

### 2.4 Configuración

```yaml
mqtt:
  fleet_manufacturer: imperial_fleet   # segmento <manufacturer> de los topics fleet/* (ver 2.5)

auto_charge:                     # genérico
  battery_floor: 20
  priority: 10
  abort_cooldown_s: 60

assigner: { prefer_not_charging: true }

drivers:                         # defaults por marca (opcional)
  mir:
    mission_group: "mirur-tknika"
    charge_mission: "Carga en estación MIRUR"
    # positions_allowlist: [...]

robots:
  mir-1:
    driver: mir                  # default "mir" si falta → el fleet.yaml actual sigue valiendo
    manufacturer: MiR            # default: el del driver
    battery_min: 25              # genérico
    host: 192.168.15.5           # ↓ resto lo interpreta el driver
    actions:
      coger: { mission: "coger" }
```

- `FleetConfig.robots[serial]` pasa a `RobotConfig(serial, driver, manufacturer,
  battery_min, raw: dict)`; el driver parsea `raw` fusionado con `drivers.<name>`.
- Sin compatibilidad hacia atrás: `mission_group`, `positions_allowlist` y
  `auto_charge.mission` desaparecen de la raíz (error claro al arrancar si
  siguen ahí). Solo existe un `fleet.yaml` y se actualiza en la misma fase.
- Env: `MIR_HOST_<SERIAL>`/`MIR_AUTH_<SERIAL>` los resuelve el driver MiR;
  el core no conoce ninguna variable de marca.

### 2.5 MQTT con varios `manufacturer`

Norma (VDA 5050 v3 §6.2): topic `interfaceName/majorVersion/manufacturer/serialNumber/topic`,
con `manufacturer` = "Manufacturer of the mobile robot". Es decir, **cada robot
publica bajo su fabricante**: `vda5050/v3/MiR/mir-1/…`, `vda5050/v3/OMRON/ld-1/…`.
El FM no impone un manufacturer único.

`fleet/*` no existe en la norma (la asignación a flota queda fuera de su
alcance); como `fleet` no es un robot, el segmento `manufacturer` es un
nombre de flota propio: **`imperial_fleet`** →
`vda5050/v3/imperial_fleet/fleet/order` y `…/fleet/order_response`. El
emisor (Node-RED) debe actualizar el topic y el header (`manufacturer:
"imperial_fleet", serialNumber: "fleet"`).

- `MqttBus` recibe `{serial: manufacturer}` en vez de un string; el Last Will
  y `publish_raw`/`next_header` usan el del robot.
- `Dispatcher.subscribe`: `vda5050/v3/+/+/order` y `+/+/instantActions`.
  `handle()` comprueba que `(manufacturer, serial)` del topic coincide con un
  robot configurado; si no, log DEBUG y se ignora.
- `fleet/*` usa `mqtt.fleet_manufacturer` (`imperial_fleet`). `validate_header` no cambia
  (header ≡ topic sigue siendo la regla).
- `HeaderCounter` ya indexa por topic completo: no hay colisión.

### 2.6 Driver simulado (`fm/adapters/sim/`)

Un driver sin hardware que ejecuta cualquier `actionType` con una duración
configurable (`duration_s`), descarga batería, y puede forzar fallos
(`fail_actions: [dejar]`). Sirve para:

- tests de integración del core end-to-end sin robots;
- demostrar que la interfaz basta para una marca "distinta" (no tiene
  missions ni GUIDs);
- material didáctico: `fleet.yaml` con `mir-1` real + `sim-1` simulado.

Es la prueba real de que la abstracción funciona; si el sim no cabe en el
Protocol, el Protocol está mal.

## 3. Fases

Cada fase deja `pytest` en verde y el FM funcionando con los MiR igual que hoy.

| # | Fase | Toca | Riesgo |
|---|---|---|---|
| 1 | **Tipos y frontera.** Crear `adapters/base.py` (`Telemetry`, `Job`, `RobotDriver`). Mover `StateOverlay` a `vda5050/`, `pick_action` a `orders.py`. `to_vda_state(header, telemetry, overlay)` en el core. | base.py, vda5050/state.py, adapters/mir.py, tests | bajo |
| 2 | **`MirDriver`.** Reorganizar en `adapters/mir/`; el driver envuelve `MirClient` + índices + `mission_queue`. `mir_client.py` queda como shim. `Robot` y `OrderTracker` pasan a usar el driver. `run_fm.py` deja de importar nada de MiR. | robot.py, orders.py, run_fm.py, adapters/mir/* | medio (es donde vive la lógica probada con robots) |
| 3 | **Config y registro.** `robots.<s>.driver`, bloque `drivers:`, `make_driver()`; nuevo `fleet.yaml`. | config.py, adapters/__init__.py, config/fleet.yaml, README | bajo |
| 4 | **Manufacturer por robot.** `MqttBus` y `Dispatcher` con `+/+/`, `imperial_fleet`; actualizar `scripts/send_fleet_order.py` y el flujo Node-RED. | mqtt_bus.py, fleet.py, header.py, scripts | bajo |
| 5 | **Driver `sim` + tests de contrato.** `tests/adapters/contract.py` parametrizado con cada driver (`sim` y `mir` con `_FakeClient`): connect → poll → translate → execute → job_status. | adapters/sim/*, tests | bajo |
| 6 | **Validación con robots** (misma prueba que el 2026-09-16 13:25) y docs: `docs/avance.md` (decisiones 22+), README sección "Añadir una marca", `CLAUDE.md` §arquitectura. | docs | — |

Orden recomendado 1→2→3→4→5→6. Las fases 3 y 4 son independientes de la 5.
Estimación: 1–2 días de trabajo, la fase 2 es la mitad.

## 4. Impacto en tests existentes

- `test_orders.py`: `FakeClient(["Executing","Done"])` → `FakeDriver` con la
  misma secuencia devuelta por `job_status`. Cambio mecánico.
- `test_vda_state.py`: `to_vda_state` recibe `Telemetry` en vez de
  `MirStatus`; añadir un test `MirStatus → Telemetry` en `tests/adapters/test_mir.py`.
- `test_mir_client.py`: sin cambios (o cambia el import al nuevo paquete).
- `test_assigner.py`, `test_header.py`: sin cambios.

## 5. Fuera de alcance (pero el diseño lo deja preparado)

- H4 auto-carga: el core llama a `driver.charge_job()` cuando
  `battery < battery_floor`; un driver sin dock devuelve `None`.
- H5 instantActions: `cancelOrder` → `driver.cancel(job_id)`; `startPause`/
  `stopPause` requerirán `pause()/resume()` en el Protocol — se añaden
  entonces, con default "no soportado" → `errors[]`.
- Factsheet: `driver.factsheet() -> dict` opcional; el core rellena el resto.
- Orders con varias actions/nodos y edges reales (navegación VDA nativa):
  cambio de contrato, no de driver.

## 6. Decisiones cerradas (2026-09-17)

1. **Manufacturer por robot**, según la norma. La order de flota llega al FM y
   este decide solo por condiciones (batería, ocupado, cargando…), sin
   importar la marca. Topics de flota bajo `imperial_fleet`.
2. **Cambio completo del `fleet.yaml`**, sin capa de compatibilidad.
3. **Driver `sim` incluido** en esta tanda (fase 5).
