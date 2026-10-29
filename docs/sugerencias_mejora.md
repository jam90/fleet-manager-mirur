# Sugerencias de mejora

Revisión del 2026-10-29 basada en el código local, `README.md`, los planes de
`docs/` y `docs/avance.md` (último avance registrado: 2026-10-23).
Son propuestas para valorar; no se han incorporado al plan ni implementado.

> **Valorado el 2026-10-29** (ver `docs/avance.md`, entrada de esa fecha).
> Pasan a pendientes: punto 1 → **#30**; punto 2 → **#31**; de punto 3,
> solo agrupar los errores de `order_errors` → **#32**. El resto
> (presupuesto por tick, modo mantenimiento, antigüedad del estado,
> diagnóstico exportable) se queda aquí como sugerencia.

## Valoración del avance

El proyecto está bastante avanzado para gestionar los dos MiR. Mantendría
la arquitectura actual: core independiente de la marca, traducción separada
de la ejecución, driver simulado y una web que utiliza el mismo circuito
MQTT que los demás clientes. La administración del catálogo también ha
avanzado mucho: crear, capturar, sincronizar y configurar forman ya un flujo
completo.

La prioridad propuesta es reforzar la fiabilidad antes de ampliar funciones.

## Cambios que priorizaría

### 1. Recuperar las suscripciones MQTT al reconectar

Es el problema más concreto encontrado en la revisión. El cliente utiliza
`clean_session=True`, pero las suscripciones se realizan al arrancar;
`MqttBus._on_connect()` publica `ONLINE` sin volver a suscribirse. Tras una
desconexión, el FM puede recuperar la publicación de estados y dejar de
recibir órdenes.

Propuesta: guardar las suscripciones y renovarlas en cada conexión.
Comprobar el comportamiento desconectando y recuperando un broker de
pruebas, sin hardware, y verificando que se siguen recibiendo órdenes.

Referencias: `fm/mqtt_bus.py`, `fm/fleet.py::Dispatcher.subscribe()` y
`run_fm.py`.

### 2. Ampliar la protección frente a órdenes duplicadas

Actualmente solo se recuerda la última `fleet/order` aceptada. Si llega A,
después B y luego se reenvía A, A podría ejecutarse otra vez en un robot
disponible. La limitación está documentada como decisión: esta sugerencia
propone revisarla antes de integrar Node-RED, no tratarla como una función
olvidada.

Propuesta: mantener un registro acotado de órdenes aceptadas, identificado
por `(orderId, orderUpdateId)`, con sus asignaciones. Permitiría reconocer
reenvíos y devolver el ACK anterior sin repetir la ejecución. Habría que
definir el tamaño o plazo de retención y qué hacer si se reutiliza la misma
clave con contenido diferente. Los rechazos por falta de disponibilidad
deben seguir permitiendo reintentar la orden.

La recuperación tras reinicio sería una segunda fase: la persistencia está
expresamente fuera del alcance inicial y añadirla supondría revisar ese
alcance.

Referencias: `fm/fleet.py::Dispatcher.last_fleet_order` y el contrato de
`fleet/order_response` en `README.md`.

### 3. Acotar el trabajo de recepción por tick

`Dispatcher.drain()` consume mensajes hasta vaciar una cola sin límite.
Una ráfaga sostenida puede retrasar la telemetría y el resto del bucle.

Propuesta: establecer un presupuesto de mensajes o tiempo de procesamiento
por tick, dejando el resto para el siguiente. Definir también cómo limitar
la cola y cómo informar de una saturación, sin descartar órdenes
silenciosamente.

Agrupar los errores repetidos ayudaría igualmente: actualmente cada rechazo
añade una entrada a `OrderTracker.order_errors`, que se conserva hasta
aceptar otra orden. Evitar que rechazos idénticos hagan crecer
indefinidamente el estado publicado.

Referencias: `fm/fleet.py::Dispatcher.drain()`, `fm/mqtt_bus.py::MqttBus.inbox`
y `fm/orders.py::OrderTracker.reject()`.

## Mejoras nuevas para valorar

No se han encontrado recogidas explícitamente como pendientes en la
documentación revisada.

### Modo mantenimiento por robot

Activarlo sin quitar el robot de la configuración. El robot seguiría
mostrando telemetría, pero quedaría excluido de nuevas asignaciones mientras
se calibra o interviene. Definir su relación con las órdenes dirigidas
directamente al robot, la auto-carga y una orden ya en curso; entrar en
mantenimiento no debería abortar implícitamente el trabajo actual.

### Indicador de antigüedad del estado

Mostrar cuándo llegó la última telemetría y marcarla como desactualizada.
La conexión del navegador al FM, la del FM al broker y la del FM al robot
representan situaciones distintas. El indicador permitiría interpretar los
datos que quedan visibles durante una pérdida de comunicación.

### Informe de diagnóstico exportable

Incluir versiones, configuración sin secretos, últimas órdenes y errores.
Serviría para investigar incidencias y compartirlas entre compañeros sin
recopilar capturas manualmente. Si requiere un historial adicional,
mantenerlo acotado y definir si solo vive en memoria.

## Funciones ya contempladas

No se proponen como novedades:

- Avisos por duración anómala de misión: pendiente #5.
- Mapa de flota: pendiente #26.
- Validación de mensajes con schemas en runtime: pendiente #6.
- Mejoras de sincronización: pendiente #19.
- Pruebas reales de recuperación de robots y recarga de configuración:
  pendientes #11 y #22, entre otras comprobaciones de hardware.
- Integración del emisor Node-RED: pendiente #2, a cargo de otro compañero.

No añadiría ahora cola interna de órdenes ni coordinación de tráfico:
ambas están fuera del alcance actual. La autenticación de administración
también está mencionada en `docs/plan-admin.md`; no es una sugerencia nueva.

## Orden recomendado

1. Corregir la recuperación de suscripciones MQTT.
2. Revisar la protección frente a duplicados.
3. Cerrar las pruebas reales pendientes de recuperación y recarga.
4. Integrar con Node-RED.

El presupuesto de recepción por tick puede abordarse junto con los dos
primeros puntos. El modo mantenimiento, el indicador de antigüedad y el
diagnóstico exportable quedarían para una siguiente iteración.

## Alcance y límites de la revisión

- La revisión no modificó código ni contactó con los robots.
- Se ejecutó la suite con
  `env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q`.
  Avanzó sin fallos visibles, pero quedó sin terminar y se interrumpió.
  No se puede afirmar que la suite completa pase ni atribuir una causa al
  bloqueo sin investigarlo.
- Había dos borradores locales de `cuadro_electrico` sin seguimiento en Git:
  `catalog/missions/imperialfleet-mission000-01-0001-012.json` y
  `catalog/positions/imperialfleet-position00-01-0001-026.json`.
  Parte del estado local todavía no estaba versionado.
