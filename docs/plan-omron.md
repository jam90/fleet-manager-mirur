# Plan: driver OMRON (ARCL)

Objetivo: segundo driver real en `fm/adapters/omron/`, para un OMRON LD,
sin tocar el core. Sirve para comprobar que la frontera de
`fm/adapters/base.py` aguanta una marca que no se parece al MiR.

Estado: **en diseño, sin código**. Fecha: 2026-11-20. Decisiones 197–198
en `docs/avance.md`.

Lo de abajo sale de un LD de otro centro de formación (firmware
`SNG Robot-8.2.7`), conectado por cable y consultado **solo con comandos
de lectura**. La IP y la clave de ARCL van en `.env`, nunca aquí: el repo
es público.

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
| mission con parámetros | macro template (`executeMacroTemplate`) | El LD probado no tiene ninguno; se crean en MobilePlanner. |
| `POST mission_queue` | `queuePickup <goal> [prioridad] [job_id]` | **El `job_id` lo pone el FM**: sirve de etiqueta para `find_job` tras reiniciar (decisión 161) sin depender de `description`. |
| mission de varios pasos | `queueMulti <n_goals> <campos_por_goal> <goal1> <args1> … [job_id]` | |
| `GET mission_queue/<id>` | `queueQueryLocal <tipo> <valor>` | Estados `Pending/Available/InProgress/Completed/Cancelled/Interrupted/Failed` → `JobStatus`. |
| `DELETE mission_queue/<id>` | `queueCancelLocal <tipo> <valor>` | |
| `PUT status 4/3` (pausa) | `applicationBlockDrivingSet <nombre> "<corto>" "<largo>"` / `applicationBlockDrivingClear <nombre>` | Lleva el motivo escrito, que el robot enseña. |
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

El FM no lo duplica: `charge_job()` devuelve `None`, no se configura
`idle_return` para el OMRON, y mientras esté yendo al dock o cargando
cuenta como `busy`. `battery_min` del robot por encima de
`AutoDockStateOfCharge`, por la misma histéresis del §6 del brief.

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

## 5. Por comprobar en el robot

Sin mover el robot:

- Formato exacto de `queueQueryLocal` y de los `QueueUpdate:` asíncronos.
- Qué devuelve `status` con la seta pulsada, en manual y con un fallo.

Con movimiento (con permiso del usuario y el robot localizado antes,
desde MobilePlanner o con `localizeAtGoal` encima de un goal):

- `queuePickup <goal> 10 <job_id>`: seguimiento hasta `Completed`,
  cancelación a medias y pausa con `applicationBlockDriving*`.
- Si un trabajo nuevo saca al robot del dock antes de terminar de cargar
  con `DockUntilDoneCharging true` (la trampa del §7 del brief).

## 6. Orden de trabajo

1. Cliente ARCL (`fm/adapters/omron/client.py`): socket con login,
   reconexión, una función por comando con el nombre del comando ARCL, y
   lectura de respuestas hasta su línea final. Tests con un servidor
   ARCL falso en local.
2. `driver.py`: `poll` (`status` + `getInfo`), `translate`, `execute`,
   `job_status`, `cancel`, `pause`/`resume`, `charge_job` → `None`,
   `extra_state` con el autodock. Pasar `tests/adapters/test_contract.py`.
3. Formato del catálogo por marca (decisión 197) en `fm/config.py` y el
   driver MiR.
4. Web: ficha del robot OMRON (autodock) y #44.
5. Prueba con el robot.
