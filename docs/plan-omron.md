# Plan: driver OMRON (ARCL)

Objetivo: segundo driver real en `fm/adapters/omron/`, para un OMRON LD,
sin tocar el core. Sirve para comprobar que la frontera de
`fm/adapters/base.py` aguanta una marca que no se parece al MiR.

Estado: **driver hecho y probado con el LD real** desde un FM de prueba
(2026-11-20): un macro template con parámetros mandado por `fleet/order` y
desde la web. Decisiones 197–205 en `docs/avance.md`; lo que falta, en el
punto #43 de su lista de pendientes.

Lo de abajo sale de un LD de otro centro de formación (firmware
`SNG Robot-8.2.7`): primero solo con comandos de lectura y después, con
permiso, moviéndolo. La IP y la clave de ARCL van en `.env`, nunca aquí:
el repo es público.

## 1. Cómo se habla con un OMRON

- **ARCL**: protocolo de texto por TCP, puerto **7171**. Al conectar pide
  `Enter password:`; tras la clave, cada comando es una línea y la
  respuesta son una o varias líneas, que suelen acabar en `End of …`. Por
  el mismo socket llegan además mensajes asíncronos (`QueueUpdate: …`,
  cambios de estado). El 7272 también está abierto (sin comprobar qué es).
- **SetNetGo** (web, `https://<ip>/`): configuración del sistema. El
  paquete *debug info* (`/cgi-bin/createDebugInfo.cgi`) trae los mapas
  (`.map`), la configuración y los logs.
- **MobilePlanner** (programa de escritorio): donde se dibuja el mapa y se
  crean goals, rutas, macros y macro templates. Es el equivalente a la web
  del MiR para crear cosas.
- Los goals, las rutas, las macros y los sectores viven **dentro del
  fichero `.map`**. Se identifican por nombre, único en el mapa: no hay
  GUIDs.

## 2. Traducción MiR ↔ OMRON

| MiR (REST) | OMRON (ARCL) | Notas |
|---|---|---|
| position | goal (`getGoals`, `mapObjectInfo <goal>` da x y θ) | Nombre único en el mapa. |
| mission sin parámetros | macro (`executeMacro`) o ruta (`patrolOnce <ruta>`) | |
| mission con parámetros | macro template (`executeMacroTemplate <nombre> $1 $2…`) | Se crean en MobilePlanner; parámetros por posición, los de texto entre comillas. Probado (decisión 201). `getMacroTemplates` no los lista. |
| `POST mission_queue` | `queuePickup <goal> [prioridad] [job_id]` | **El `job_id` lo pone el FM**: sirve de etiqueta para `find_job` tras reiniciar (decisión 161) sin depender de `description`. |
| mission de varios pasos | `queueMulti <n_goals> <campos_por_goal> <goal1> <args1> … [job_id]` | |
| `GET mission_queue/<id>` | `queueQuery jobid <id>` (**sin** Local) y los `QueueUpdate:` | Fases vistas: `Pending` → `InProgress UnAllocated/Allocated/Driving/After` → `Completed`; `Cancelling` → `Cancelled`. Con Local, un trabajo devuelto a la cola sin robot no sale (decisión 201). |
| `DELETE mission_queue/<id>` | `queueCancelLocal <tipo> <valor>` | |
| `PUT status 4/3` (pausa) | `applicationBlockDrivingSet <nombre> "<corto>" "<largo>"` / `applicationBlockDrivingClear <nombre>` | No es una pausa: interrumpe y devuelve el trabajo a la cola; al quitarlo, el robot lo retoma. Corta una macro (decisión 202). |
| `GET status` | `status` / `oneLineStatus` + `getInfo <nombre>` | `Location` en **mm, mm, grados** → m y rad. |
| `battery_percentage` | `StateOfCharge` (también `getInfo ChargeEstimate`) | |
| `charging` (deducido) | `DockingState: Docked` + `ChargeState` (`Bulk/Overcharge/Float`), `getInfo OnCharger` | El OMRON lo dice directamente. |
| `map_id` | `getConfigSectionValues Files` → `Map <fichero>.map` | |
| localizado | `LocalizationScore` / `getInfo LaserScore` | Con 0, la cola lo da por `UnAvailable Lost` → `available=False`. |
| `errors[]` | `faultsGet`, `queryFaults` | |
| llave `auto/manual` | no hay | Ver qué da `status` con la seta o en manual. |
| zonas del mapa (`in_zone`) | sectores (`mapObjectList <tipo>`) | |
| registros PLC | `inputQuery`, `outputQuery`, Data Store | Para herramientas, más adelante. |
| mission de carga | autodock del propio robot (decisión 198) | |
| Sincronización, Crear, Retirar | sin equivalente | Todo va en el `.map` y se edita en MobilePlanner. El driver OMRON no ofrece esas pestañas. |

## 3. Carga (decisión 198)

El LD ya se carga solo (sección `Docking` de su configuración):

- `AutoDock true`, `AutoDockStateOfCharge 25`: por debajo del 25 % va al
  cargador.
- `AutoDockIdleTime 15`: también va tras 15 min parado (hace de vuelta a
  la espera).
- `DockUntilDoneCharging true`: se queda hasta terminar de cargar.

El FM no lo duplica: `charge_job()` devuelve `None` y no se configura
`idle_return` para el OMRON. `battery_min` del robot por encima de
`AutoDockStateOfCharge`, por la misma histéresis del §6 del brief.

Visto en el LD (decisión 203): cargando en el dock **no** está ocupado: un
trabajo de la cola lo saca del dock aunque no haya terminado de cargar. Lo
que cuenta como ocupado por otro (`foreign_busy`) es ir al dock, salir de
él o moverse sin que lo pida el FM. Un dock fallido no se anuncia
(decisión 205).

La web enseña en la ficha del robot OMRON si el autodock está activado y
sus valores (`AutoDock`, `AutoDockStateOfCharge`, `AutoDockIdleTime`,
`DockUntilDoneCharging`), leídos del robot con `getConfigSectionValues
Docking`. Solo lectura: se cambian en MobilePlanner.

## 4. Catálogo de actions con varias marcas (decisión 197)

Cada action del catálogo lleva una definición por marca:

```yaml
actions:
  ir_a:
    position_inputs: [target_pos]
    mir:   { mission: "Ir a posición" }
    omron: { goal_input: target_pos }     # queuePickup <goal>
```

Lo común a las marcas (los `*_inputs` y sus validaciones) queda en la
action; lo de cada marca, en su bloque. Un robot puede habilitar una
action solo si su driver tiene bloque en ella. Hoy `mission` está en la
raíz de la action (decisión 92): el paso al formato nuevo se hará cuando
se escriba el driver, con el mismo tratamiento que tuvieron los formatos
antiguos (error al arrancar con la pista de cómo migrar, o migración en
el Configurador).

**Pendiente (#44):** cómo enseña y edita esto la interfaz. Los nombres no
coinciden entre marcas (position / goal, mission / macro / ruta, zona /
sector) y el Configurador, el Catálogo y las fichas de robot hablan hoy
en términos del MiR.

## 5. Visto en el robot (2026-11-20)

Comprobado (detalle en la entrada del 2026-11-20 de `docs/avance.md`):
formatos de la cola y sus fases, cancelación, pausa (que es un fallo que
devuelve el trabajo a la cola, decisión 202), seta, dock (textos, tiempos,
reintentos y fallos), movimientos a mano, crear goals con `mapObjectUpdate`
(decisión 204), macros y macro templates con parámetros (decisión 201), y
que un trabajo saca al robot del dock aunque esté cargando (decisión 203).

Sin ver todavía: el autodock por batería baja, `status` en modo manual,
`queueMulti` con varios goals, y qué pasa con un trabajo de la cola si el
robot pierde la localización a medio camino.

## 6. Orden de trabajo

0. ~~OMRON digital~~ (hecho el 2026-11-20, decisión 199): servidor ARCL
   que contesta como el LD, en `simuladores/omron/` (ver su README). El
   driver se desarrolla contra él; lo marcado como supuesto se corrige
   cuando se pueda probar con el robot.
1. ~~Cliente ARCL~~ (`fm/adapters/omron/client.py`, decisión 200): un
   socket, separa respuestas de mensajes repartidos a todos.
2. ~~`driver.py`~~ (decisiones 200–203): goals de la cola, macros y
   templates; pasa el contrato contra el digital
   (`tests/adapters/test_omron_driver.py`, `test_contract.py`).
3. Formato del catálogo por marca (decisión 197): el OMRON ya lee su bloque
   `omron:`; falta llevar el del MiR a `mir:` y decidir la interfaz (#44).
4. Web: ficha del robot OMRON (autodock) y #44.
5. ~~Prueba con el robot~~ (2026-11-20). Pendiente probar desde el FM
   `ir_a_omron`, `cancelOrder` y `startPause`.
6. Crear goals desde la pestaña Crear (decisión 204) y `find_job` para
   retomar tras un reinicio.
