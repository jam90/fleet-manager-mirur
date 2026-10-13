# Plan: configurador del FM y sincronización de la flota

Objetivo: dos pestañas nuevas en la web del FM, separadas de la interfaz de
uso diario de los operarios:

1. **Configurador del FM**: editar desde el navegador lo que hoy se edita a
   mano en `config/fleet.yaml` (umbrales, qué robot hace qué action,
   parámetros de cada action, alta de robots).
2. **Sincronización**: llevar a los robots los recursos que crea el usuario
   en la web del MiR (**sessions, mapas, positions, zonas y missions**) con
   **GUIDs comunes en toda la flota**, para que una mission programada una
   vez en Blockly llegue a los 2… o 50 robots que la necesiten.

Estado: **en diseño**. Nada programado en el FM. Fase 0 (pruebas con robot)
**en curso desde el 2026-10-13**: sessions y mapas probados y estándar de
GUID fijado (§4); positions, zonas y missions por probar (§6).

Decisiones tomadas con el usuario (2026-10-11):

- Dos pestañas nuevas, distintas de la de operación: un operario básico no
  tiene por qué tocar esto.
- **No hay editor de missions.** La lógica se programa en el Blockly de un
  MiR; el FM solo la captura y la propaga.
- La sincronización es **puntual**, no continua: se usa cuando hay una
  mission nueva, una position nueva o llega un MiR nuevo.
- Hay que diseñar pensando en **N robots (p. ej. 50)**, no solo en 2.
- El usuario **no edita GUIDs**: el FM asigna el siguiente que toque según
  un estándar propio (§4).
- Según el usuario, las actions de base del MiR (los tipos de bloque) tienen
  los mismos GUIDs en todos los robots; solo hay que controlar lo que crea
  el usuario: missions, mapas y positions.
- Seguridad: por ahora la herramienta solo la usa el profesor; **no es
  prioritaria**. Se deja anotado (§7) y no bloquea nada.

## 1. Idea central: el configurador dice qué hace falta, la sincronización lo pone

```
 Configurador (fleet.yaml)                Sincronización
 ─────────────────────────                ──────────────
 mir-37 tiene habilitada `coger`  ──────► mir-37 necesita la mission "coger",
                                          las positions que usa y su mapa
```

Con 50 robots no se eligen los destinos a mano: se calculan a partir de las
actions habilitadas en cada robot. El configurador es el "qué" y la
sincronización el "cómo".

## 2. Configurador del FM

### 2.1 Cambio previo de formato de `fleet.yaml`: catálogo de actions

Hoy cada action se define **una vez por robot** con el mismo contenido
(`coger`, `dejar`, `prueba_parametros_v2`… repetidos en mir-1 y mir-2).
Con N robots eso no se puede mantener ni pintar en una UI. Nuevo formato:

```yaml
actions:                      # catálogo único: cada action se define una vez
  coger:   { mission: "coger" }
  dejar:   { mission: "dejar" }
  prueba_parametros_v2:
    mission: "MIRUR-prueba-parámetros-v2"
    position_inputs: [target_pos]
    positions_allowlist: [Deshecho, Montaje, Area_calidad]
    number_inputs: { n_piezas_pedidas: { min: 1, max: 10, step: 1 } }

robots:
  mir-1: { host: 192.168.15.5,  battery_min: 25, actions: [coger, dejar, prueba_parametros_v2] }
  mir-2: { host: 192.168.15.15, battery_min: 25, actions: [coger, dejar, recoger_pieza] }
```

- Un robot puede **sobrescribir** campos de una action si de verdad hace
  falta (forma dict en vez de lista); lo normal es la lista de nombres.
- El formato actual (dict de actions por robot) sigue funcionando un
  tiempo o da error al arrancar con la pista de cómo migrar, como se hizo
  en la fase 3 de drivers. A decidir al implementarlo.
- Esta fase va **sin UI**, solo cargador + tests. Ya mejora el fichero aunque
  la UI no llegue.

### 2.2 Pestaña

- **Matriz action × robot** con casillas = "qué MiR puede hacer qué
  mission". Al marcar una casilla de un robot que no tiene la mission, se
  avisa y se enlaza con la pestaña de sincronización.
- **Umbrales**: `auto_charge.*`, `battery_min` por robot, `assigner.*`.
- **Catálogo de actions**: mission asociada, `position_inputs`,
  `number_inputs` (con rango), `required_inputs`, allowlist.
- **Alta y baja de robots**: serial, driver, host.

### 2.3 Guardar y aplicar

- **Guardar sin perder comentarios:** PyYAML los descarta al reescribir y
  `fleet.yaml` está comentado a propósito (didáctico). Usar `ruamel.yaml`
  (dependencia nueva) en modo ida y vuelta.
- Antes de escribir: **validar con el mismo cargador del arranque** (si
  falla, no se guarda y se muestra el error) y dejar copia
  `fleet.yaml.<fecha>.bak`.
- **Aplicar en caliente** (preferencia del desarrollador, pendiente de
  confirmar): la web no toca el estado; encola "recargar config" y el hilo
  principal la aplica entre ticks (decisión 14). Después: volver a resolver
  los índices nombre → GUID de los robots afectados (en su hilo, como el
  poll) y republicar el `factsheet` (cambian sus `agvActions`). Una order
  en curso no se ve afectada: su `Job` ya está traducido.

## 3. Sincronización

### 3.1 Catálogo en el repo entre el robot y la flota

Con 2 robots basta con copiar de uno a otro. Con 50, el robot de origen
puede estar apagado, roto o editado por error, y no queda historial. Se
propone un paso intermedio:

```
  Blockly en un MiR           catalog/ (en git)             flota
  ─────────────────  capturar ─────────────────  desplegar  ──────────────
  creas/editas la    ───────► maps/*.json        ────────►  mir-1 … mir-50
  mission o position          positions/*.json              (solo los que
                              missions/*.json                lo necesitan)
```

- **Capturar:** eliges un robot y un recurso; el FM lo descarga, le asigna
  GUID estándar si aún no lo tiene y lo guarda como fichero JSON legible.
- **Desplegar:** el FM compara cada robot con el catálogo y aplica solo las
  diferencias.

Por qué:

- El **contador de GUIDs sale del catálogo** (máximo + 1): no hace falta
  tener los 50 robots encendidos para crear un recurso, y no hay riesgo de
  colisión con un robot apagado.
- Un **MiR nuevo se monta desde ficheros**, sin depender de otro robot.
- **Historial y marcha atrás con git**; además el catálogo enseña en clase
  qué es una mission por dentro.
- No contradice "sin persistencia" (§1 del brief): el catálogo es
  configuración, como `fleet.yaml`, no estado de ejecución.

### 3.2 Casos de uso

1. **Mission nueva:** programarla en Blockly en cualquier MiR → capturar →
   habilitarla como action en el configurador → desplegar.
2. **Position nueva:** crearla en un MiR → capturar → desplegar a los
   robots con ese mapa.
3. **MiR nuevo:** alta en el configurador (serial, host, actions) →
   desplegar: recibe mapa, positions y missions según sus actions.
4. **Adoptar lo existente** (una vez): las missions y positions actuales
   tienen GUIDs aleatorios distintos por robot. Capturarlas les da GUID
   estándar; al desplegar se crean con él en todos los robots (el de
   origen incluido) y se borran las antiguas.

### 3.3 Despliegue a escala

- **Orden de dependencias:** session → mapa → positions y zonas →
  missions (el mapa necesita su session en el robot, `session_id`; las
  positions y zonas, su mapa; las missions, sus positions). Si una mission
  usa una position que no existe con el mismo GUID en el destino, se
  **bloquea** con un mensaje claro en vez de copiar a medias.
- **Comparar antes de escribir:** cada recurso tiene un hash de su
  contenido normalizado; si el robot ya lo tiene idéntico, no se toca.
  Repetir un despliegue siempre es seguro y solo arregla lo que faltaba o
  falló (idempotente).
- **Vista previa obligatoria** antes de aplicar: "en mir-2 se crea X; en
  mir-7 se sustituye Y, que es distinta". Sustituir = borrar + crear, lo
  que rompe las missions del destino que la usen como sub-mission: avisarlo.
- **Varios robots a la vez, con límite** (p. ej. 8 simultáneos), en hilos
  propios: nunca en el bucle de 1 Hz. Progreso por robot en la UI por el
  websocket existente.
- **Robots ocupados (`busy`) u offline:** se saltan y quedan como
  "pendiente"; se vuelve a desplegar más tarde.
- Tras desplegar a un robot: volver a resolver sus índices nombre → GUID.

### 3.4 Pestaña

- **Vista de flota:** una fila por robot ("al día", "3 diferencias",
  "offline", "pendiente"); al pulsarla, el detalle por recurso. Con 50
  robots no cabe una matriz recursos × robots.
- **Catálogo:** lista de mapas, positions y missions capturados, con su
  GUID en solo lectura, el robot del que se capturaron y la fecha.
- Botones: capturar desde robot, vista previa, desplegar (todos / robots
  seleccionados).

## 4. Estándar de GUID

Fijado el 2026-10-13 (decisiones 71–72 de `docs/avance.md`). Sustituye a
la propuesta hexadecimal `4d495255-…` del 2026-10-11: el MiR no valida el
formato UUID, así que se elige un GUID **legible**, para que quien haga una
petición sepa qué es cada recurso con solo ver su GUID.

Qué exige el MiR (probado con `POST /sessions` en mir-2; falta confirmarlo
en los demás endpoints):

- **Exactamente 36 caracteres:** con 35 o 37 responde `400
  invalid_input_data` (`input_string_too_short` / `input_string_too_long`).
- **Cualquier contenido:** aceptó `estoesunapruebadequepuedoponeresto01`
  (sin guiones ni hexadecimal). Sus propios GUIDs de fábrica tampoco son
  hexadecimales (`mirconst-guid-0000-0001-maps00000000`).
- **Único:** repetir un GUID existente da `409 duplicate_entry` y no toca
  el recurso que ya había.

```
imperialfleet-position00-01-0001-012
└── prefijo ─┘ └─ tipo ─┘ └┘ └──┘ └─┘
     13 car.     10 car. inst padre número
```

- **`imperialfleet`**: marca lo creado por el FM (el nombre de la flota).
  Lo que no empiece así viene de la web del MiR o es de fábrica
  (`mirconst-…`), y el FM no lo toca.
- **tipo**: nombre del recurso en inglés, como su endpoint, rellenado con
  `0` hasta 10 caracteres.
- **inst**: instalación, 2 cifras; `01` = esta (Tknika, MIRUR). Evita
  choques si dos FM con catálogos distintos comparten un robot.
- **padre**: número del recurso padre, 4 cifras; `0000` si no tiene.
- **número**: contador de 3 cifras (hasta 999).
- Solo minúsculas, cifras y guiones: el GUID va dentro de las URL
  (`GET /maps/<guid>`) y caracteres como `_` no se han probado.

| tipo | Recurso | Endpoint | padre | Numeración | Ejemplo |
|---|---|---|---|---|---|
| `session000` | session (site) | `/sessions` | — | instalación | `imperialfleet-session000-01-0000-001` |
| `map0000000` | mapa | `/maps` | session | instalación | `imperialfleet-map0000000-01-0001-001` |
| `position00` | position | `/positions` | mapa | por padre | `imperialfleet-position00-01-0001-012` |
| `zone000000` | zona | `/zones` | mapa | por padre | `imperialfleet-zone000000-01-0001-002` |
| `pathguide0` | path_guide | `/path_guides` | mapa | por padre | `imperialfleet-pathguide0-01-0001-001` |
| `missiongrp` | mission_group | `/mission_groups` | — | instalación | `imperialfleet-missiongrp-01-0000-001` |
| `mission000` | mission | `/missions` | grupo | instalación | `imperialfleet-mission000-01-0001-005` |
| `sound00000` | sonido subido a mano | `/sounds` | — | instalación | `imperialfleet-sound00000-01-0000-001` |

Reglas de numeración:

- **Los recursos sin hijos** (position, zona, path_guide, action) **se
  numeran dentro de su padre**: "position 12 del mapa 1".
- **Los recursos que tienen hijos** (mapa, mission) **se numeran en toda la
  instalación**, y su campo padre (session, grupo) es solo informativo.
  Motivo: si el mapa se numerase dentro de su session, el mapa 1 de dos
  sessions distintas daría el mismo padre a sus positions, y la position 1
  de cada uno tendría el mismo GUID. El campo padre solo tiene sitio para
  el número del padre, no para el de su abuelo.
- **Siguiente número** = máximo de los GUIDs del catálogo con el mismo tipo
  (y el mismo padre, si se numera por padre) + 1. Se lee del GUID, no del
  padre actual: una mission que se mueve de grupo en la web del MiR
  conserva su GUID (su campo padre queda desfasado, sin más) y su número no
  se reutiliza.
- **Lo que el MiR crea solo no lleva GUID nuestro en el robot**
  (decisión 77). Al crear un marcador (tipo 11) o un cargador (tipo 20),
  el MiR crea su position de entrada (tipos 12 y 21) y, en los marcadores,
  su docking offset, con GUIDs propios. Ninguna mission los usa (revisadas
  las 62 de mir-2): usan la madre. En el catálogo, la entrada tiene GUID
  estándar solo para nombrar su fichero (`creada_por_mir: true`), y el
  docking offset va dentro del fichero de su marcador, sin GUID. Al
  desplegar se buscan por `parent_id` y se corrigen con PUT.
- **Tampoco llevan GUID nuestro las actions de una mission ni sus
  parámetros** (decisión 78): nadie los referencia desde fuera. El
  anidamiento (`scope_reference` → GUID de un parámetro de la contenedora)
  se guarda como `(priority de la contenedora, id del parámetro)` y se
  rehace al desplegar con los GUIDs que ponga el MiR.
- El usuario nunca edita un GUID; la UI lo muestra en solo lectura.
- La resolución por nombre se mantiene como respaldo: un robot editado a
  mano en la web volvería a tener GUIDs propios.

Recursos creados con este estándar:

| GUID | Recurso | Robots | Fecha |
|---|---|---|---|
| `imperialfleet-session000-01-0000-001` | session `suberri_mirur` | mir-2 | 2026-10-13 |
| `imperialfleet-map0000000-01-0001-001` | mapa `suberri_mirur` (en la session `suberri_mirur`; creado como `mapa_prueba` y renombrado por el usuario) | mir-2 | 2026-10-13 |

| `imperialfleet-position00-01-0001-001` … `-023` | las 23 positions de `suberri` de mir-2, en el mapa `suberri_mirur` (las entradas 009, 014, 016 y 018 tienen GUID del MiR) | mir-2 | 2026-10-13 |

Capturadas en `catalog/positions/` con `scripts/catalog_capture.py` y
desplegadas con `scripts/catalog_deploy.py`.

Desplegados en mir-2 (2026-10-13, con `catalog_deploy.py missions`): el grupo
`mirur-tknikav2` (`…-missiongrp-01-0000-001`), las 9 missions de
`mirur-tknika` de mir-2 más `Ir a zona de espera` de `p6-factory`, a la que
llaman (`…-mission000-01-0001-001` a `…-010`), y el sonido `piezaH2D1`
(`…-sound00000-01-0000-001`).

## 5. Arquitectura

- **Configurador (genérico):** core + `fm/web`. Escritura de config en
  `fm/config.py` (o módulo propio), validación con el cargador actual.
- **Sincronización (específica de MiR):** `fm/adapters/mir/sync.py` en dos
  partes:
  - **Planificación pura:** catálogo + índice del robot → lista de
    operaciones (crear, sustituir, nada, bloqueado). Sin red, testeable
    como `translate.py`.
  - **Ejecutor:** aplica las operaciones con el cliente REST.
- **Cliente REST:** métodos nuevos con la convención `<recurso>_<verbo>`
  (`sessions_get`, `sessions_post`, `maps_get`, `maps_post`, `zones_post`,
  `missions_post`, `missions_mission_id_get`, `missions_mission_id_delete`,
  `missions_mission_id_actions_post`, `positions_post`,
  `positions_guid_delete`…). Los nombres exactos se fijan tras la fase 0.
- Un robot `sim` no ofrece sincronización; la pestaña lo muestra como
  "no aplica".
- `catalog/` en la raíz del repo, versionado.

## 6. Fases

0. **Pruebas con robot** (en curso desde el 2026-10-13; condiciona todo §3
   y §4). Solo en **mir-2** (`192.168.15.15`), **sin lanzar missions**.
   **El mapa `suberri` y su session no se tocan nunca:** las pruebas van en
   la session `suberri_mirur` y en un mapa `mapa_prueba`.
   - ~~`POST /sessions` con un GUID elegido~~ **Hecho (2026-10-13):** se
     acepta cualquier cadena de exactamente 36 caracteres, y un GUID
     repetido da 409 (§4). Creada `suberri_mirur`.
   - `POST /missions`, `/positions`, `/mission_groups` y `/zones` con un
     GUID del estándar (§4): ¿lo aceptan igual que `/sessions`?
   - Leer `missions/<guid>/actions` de una mission con bloques anidados
     (if/loop): cómo se representan padre/hijo y si las actions dentro de
     una mission tienen GUID propio que haya que conservar o regenerar.
   - Comprobar que los tipos de action de base coinciden entre mir-1 y
     mir-2.
   - ~~**Mapas (prioritaria):** crear `mapa_prueba` por `POST /maps` con
     GUID elegido~~ **Hecho (2026-10-13):** creado en `suberri_mirur` con
     GUID `imperialfleet-map0000000-01-0001-001` y el PNG de
     `img_referencia/mapa_base/`. El robot conserva el GUID y guarda
     `base_map` byte a byte, sin convertir la imagen. Las positions, zonas
     y path_guides cuelgan del mapa y se crean aparte (siguiente prueba).
   - Si un backup/restore o una actualización del MiR conserva los GUIDs
     (puede quedar para más adelante).
1. **Nuevo formato de `fleet.yaml`** (catálogo de actions), con tests y sin UI.
2. **Pestañas en solo lectura:** matriz action × robot, umbrales, vista de
   flota de sincronización (comparando con los índices actuales por nombre).
3. **Edición en el configurador** + guardado con `ruamel.yaml` + recarga.
4. **Catálogo + GUID estándar + capturar** (missions primero).
5. **Desplegar missions** con vista previa, idempotente y con
   concurrencia limitada. Adoptar las missions existentes.
6. **Positions y mapas** (según lo que diga la fase 0 y la pregunta del §8).

## 7. Fuera de alcance (por ahora)

- Editor de missions (se programan en Blockly).
- Sincronización continua o automática: solo bajo demanda.
- Autenticación de las pestañas admin. Nota: una pestaña aparte no
  protege nada (cualquiera en la wifi puede abrir la URL). Cuando la usen
  más personas, como mínimo contraseña en `.env` para `/admin/*`.
- Otras marcas en la sincronización.

## 8. Preguntas abiertas

1. ~~¿Todos los robots usan el mismo mapa?~~ **Respondida (2026-10-11):**
   sí, el contenido del mapa ya es idéntico en los robots; solo cambia el
   GUID. Objetivo: subir el mapa por REST **fijando también su GUID**, para
   que mapa (y por tanto positions) sean idénticos en contenido y en GUID.
   Es la prueba principal de mapas de la fase 0. **Matiz (2026-10-13):**
   los dos mapas `suberri` salieron del mismo PNG, pero hoy difieren en
   2.008 píxeles (0,3 %, paredes dibujadas en el taller), y las positions
   tampoco coinciden (26 en mir-1, 23 en mir-2).
2. **¿Se usan sub-missions** (missions que llaman a otras)? Afecta al orden
   de despliegue y a los conflictos al sustituir.
3. **Aplicar config en caliente o reiniciando el FM:** propuesta en caliente
   (§2.3); confirmar.
4. **Cómo se copian hoy** las missions entre los dos robots (¿exportar e
   importar en la web del MiR?): puede dar pistas sobre el formato y sobre
   si los GUIDs se conservan. Pista (2026-10-13): cada session expone
   `/sessions/<guid>/export`; falta ver qué devuelve.

## 9. Riesgos

- Que el MiR **no acepte GUIDs elegidos** en algún recurso: el catálogo y el
  despliegue siguen siendo útiles, pero habría que mantener la resolución
  por nombre como mecanismo principal. Las sessions sí los aceptan
  (2026-10-13); falta el resto.
- Que copiar las actions de una mission por API sea frágil (bloques
  anidados, parámetros que apuntan a otros recursos): empezar por missions
  sencillas y comparar el resultado en la web del MiR.
- Sustituir una mission mientras un robot la ejecuta: por eso se saltan los
  robots `busy`.
- Borrar recursos en un robot es irreversible: vista previa obligatoria y,
  en la fase 0, trabajar solo en un grupo de pruebas.
