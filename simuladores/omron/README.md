# OMRON digital (LD por ARCL)

Un servidor ARCL que contesta como el OMRON LD real. Sirve para escribir y
probar el driver OMRON del FM (`docs/plan-omron.md`) sin tener el robot
delante, y para añadir algún día un OMRON "de mentira" a la flota.

No forma parte del FM: es un robot simulado. El FM (o `nc`, o MobilePlanner
si algún día hace falta) se conecta a él igual que al robot real.

## Arranque

```bash
env -u PYTHONPATH .venv/bin/python -m simuladores.omron            # puerto 7171, clave "digital"
env -u PYTHONPATH .venv/bin/python -m simuladores.omron --perdido  # como lo encontramos: sin localizar
env -u PYTHONPATH .venv/bin/python -m simuladores.omron --acelerar 10 --soc 27 --fuera-del-dock descanso
```

Para hablar con él a mano: `nc localhost 7171`, escribir la clave y, por
ejemplo, `status`, `getGoals`, `queuePickup habitacion1 10 PRUEBA-1`,
`queueQueryLocal jobid PRUEBA-1`, `quit`.

| Opción | Qué hace |
|---|---|
| `--port` | Puerto ARCL (7171, como el real). |
| `--password` | Clave ARCL (o `OMRON_DIGITAL_PASSWORD`). Por defecto `digital`. |
| `--map` | Fichero `.map` de MobilePlanner. Por defecto `datos/aula_ejemplo.map`. |
| `--nombre` | Nombre del robot en la cola (`Omron-LD`). |
| `--dock x,y,θ` | Pose del cargador en mm y grados (por defecto la del LD real). |
| `--soc` | Batería inicial. |
| `--perdido` | Arranca sin localizar (`LocalizationScore 0`): no coge trabajos. |
| `--fuera-del-dock GOAL` | Arranca en ese goal en vez de en el cargador. |
| `--velocidad` | mm/s en recta (1000). |
| `--acelerar` | Factor de tiempo: 10 = diez veces más rápido (batería incluida). |
| `--autodock`, `--autodock-soc`, `--autodock-idle`, `--dock-hasta-cargar` | Cambian la sección `Docking`, que en el real se toca en MobilePlanner. |

Se para con Ctrl-C o con SIGTERM.

## Qué simula

- **Sesión ARCL:** pide la clave; con una mala corta la conexión con un
  RST sin decir nada. Con la buena, la bienvenida y la lista de comandos
  del LD real (`datos/ayuda.txt`). Los comandos no distinguen mayúsculas,
  valen los alias de `help` (`qp`, `qs`…), y `goto` funciona aunque no
  salga en `help`.
- **Mensajes a todos los clientes:** `Going to …`, `Arrived at …`,
  `Interrupted: Going to …`, `Error: Failed going to goal`, las líneas
  `DockingState: …` del dock, `QueueUpdate: …`, `Executing/Completed
  macro (template) …`, `WaitState: …`, `Motors disabled`, `EStop
  pressed`, `Stopping`/`Stopped`, `Map changed`, `Fault:`/`FaultCleared:`.
- **Estado:** `status`, `oneLineStatus`, `queryDockStatus`, `queryMotors`,
  `modeQuery`, `getInfo …` y `getInfoList`, `odometer`,
  `getPoseEncoder`, `goalDistanceRemainingLocal`.
- **Mapa:** `getGoals`, `getRoutes`, `getMacros`, `getMacroTemplates`,
  `mapObjectList`, `mapObjectInfo`, `mapObjectTypeList`,
  `mapObjectTypeInfo`, leídos del `.map`. Si el mapa tiene un dock
  (`DockLynx`), el cargador está ahí y los textos llevan su nombre.
  `mapObjectUpdate` crea goals (con `Map changed`).
- **Cola:** `queuePickup <goal> [prioridad] [job_id]`, `queueMulti`,
  `queueQuery(Local)`, `queueCancel(Local)`, `queueShow`,
  `queueShowCompleted`, `queueShowRobot(Local)`. Coge el trabajo de más
  prioridad y, a igualdad, el más antiguo. Un trabajo lo saca del dock
  aunque esté cargando.
- **Macros y templates** del `.map` (`executeMacro`,
  `executeMacroTemplate <nombre> $1 $2…`): `goto`, `wait`, `gotoPoint x y θ`
  (mm y grados; llega con ese rumbo) y `MacroTask` (una macro que llama a
  otra o a un template con sus valores, como guarda MobilePlanner una macro
  hecha desde un template); el resto de tareas (`say`…) no hacen nada. Sin
  `Going to` dentro de la macro, y con un goal inexistente la macro
  "termina" sin error, como el real.
- **Movimiento directo:** `goto`, `dock`, `undock`, `patrol`, `patrolOnce`,
  `stop`, `stay`, `go`, `localizeAtGoal`. Una orden directa deja el
  trabajo de la cola `Interrupted`, y se retoma después.
- **Pausa:** `applicationBlockDrivingSet` / `Clear`, como en el real: un
  fallo que interrumpe y devuelve el trabajo a la cola sin robot.
- **Fallos de aplicación:** `applicationFaultSet` / `Clear` / `Query`,
  `faultsGet`, `queryFaults`.
- **Carga:** autodock con la sección `Docking` del LD real
  (`AutoDock`, `AutoDockStateOfCharge`, `AutoDockIdleTime`).
- **Configuración:** `getConfigSectionList` y `getConfigSectionValues` de
  `Files`, `Docking`, `General`, `Battery_1` y `ARCL server setup`, como
  las da el real (esta última enseña la clave en claro, como el real).
- **E/S:** `inputList`, `outputList`, `analogInputList`, `inputQuery`,
  `outputQuery`, `outputOn`/`Off`.
- **API MQTT** (con `--mqtt localhost:1883`, `mqtt.py`): la telemetría
  que publica la aplicación MQTT API del LD (`itk/dt/robot/status` 5
  veces por segundo, `pose`, `qmState`, `goals` y los valores del
  DataStore que se pidan), en un broker sin TLS. El driver se apunta con
  `mqtt_host`, `mqtt_port` y `mqtt_tls: false` en `fleet.yaml` y
  `OMRON_MQTT_PASSWORD` con cualquier valor (decisión 207). Los comandos
  por MQTT (`itk/cmd/arcl`, trabajos) no se simulan: el FM manda por ARCL.
  Los topics no llevan el nombre del robot: un digital con MQTT por broker.

## Comandos solo del digital

No existen en el robot real (contestaría `Unknown command`) y no salen en
`help`. Sirven para provocar lo que en el real cuesta provocar:

| Comando | Efecto |
|---|---|
| `digitalEstop on\|off` | Pulsa o suelta la seta. Al soltarla los motores siguen deshabilitados hasta `enableMotors`. |
| `digitalSoc <n>` | Pone la batería al n %. |
| `digitalLost` | Pierde la localización. |

## Visto en el LD real o supuesto

El código marca cada respuesta con `# visto en el LD` o `# supuesto`.

**Visto** (LD real, firmware `SNG Robot-8.2.7`, 2026-11-20; primero solo
leyendo y luego moviéndolo, con permiso): casi todo. La sesión y la clave,
`help`, los errores, `status` en todas las situaciones que hubo (dock,
parado, en marcha, al llegar, al fallar, con la seta, buscando el
cargador), la cola completa (formatos, fases, cancelar, pausa), el dock,
la seta, los movimientos a mano, `mapObjectUpdate`, macros y templates, y
las secciones de configuración. Detalle en `docs/avance.md` (entradas del
2026-11-20) y `docs/plan-omron.md`.

**Supuesto** (no se vio):

- Cuánto tarda en buscar el cargador y acoplarse (el real tardó ~1 min y
  a veces reintenta).
- El autodock por batería baja y por tiempo parado.
- `status` en modo manual o con otros fallos.
- `queueMulti` con varios goals (formato de la respuesta).
- Los números de `ChargeState` salvo `Float = 3`.
- Que `stop` y `stay` lo dejen 60 s sin coger trabajos.
- `gotoPoint` dentro de una macro: que no dé mensajes de `Going to point`
  ni `Arrived at` (como `goto`), y que uno mal escrito no haga nada. En el LD
  se vio solo dónde acaba el robot.
- `MacroTask`: que la macro llamada no dé sus propios `Executing` y
  `Completed` (solo la de fuera).
- Cuánto guarda el LD los trabajos terminados en la cola (el digital, todos):
  el FM no relanza uno que no encuentra tras un reinicio (decisión 214).

## Lo que no hace

- **Navegación:** va en línea recta de un punto a otro, sin mirar paredes
  ni zonas prohibidas, sin obstáculos y sin `Cannot find path`.
- Ir a un punto suelto (`Going to point x y`): eso lo manda MobilePlanner,
  no ARCL.
- `getConfigSectionInfo`, el Data Store (`getDataStore…`),
  `queueModify`, `queuePickupDropoff`, `patrolResume`, `doTask`:
  contestan `CommandError` diciendo que no están implementados.
- Secciones de configuración que no se capturaron: devuelven solo la línea
  final.
- Un solo robot por proceso y sin Enterprise Manager (como el LD probado).
  Para dos OMRON digitales, dos procesos en puertos distintos.

## Ficheros

- `mapa.py`: lectura del `.map`.
- `robot.py`: el modelo (puro, sin red): estado, movimiento, batería,
  autodock y cola.
- `arcl.py`: el intérprete de comandos ARCL.
- `servidor.py`: el socket TCP; un hilo por cliente y un reloj.
- `mqtt.py`: la API MQTT (`ApiMqtt`, pura) y su publicador en un broker.
- `datos/`: lo copiado del LD real (ayuda, secciones de configuración,
  listas de E/S y tipos de objeto) y `aula_ejemplo.map`, un mapa con los
  goals, la ruta y las paredes del LD real, sin la nube de puntos.

Tests: `tests/test_omron_digital.py` y, con el driver del FM por socket,
`tests/adapters/test_omron_driver.py`; la API MQTT, en
`tests/adapters/test_omron_mqtt.py`.
