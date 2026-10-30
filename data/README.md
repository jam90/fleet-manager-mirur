# Datos que escribe el FM mientras funciona

Aquí no hay configuración (eso es `fleet.yaml` y `catalog/`): es **estado de
funcionamiento**, y no va en git (solo este README).

- `<nombre del fleet.yaml>.sqlite`: la cola de `fleet/order` (decisión 160,
  `docs/plan-cola.md`). `fleet.sqlite` para la flota real y
  `fleet-sim.sqlite` para la simulada: no comparten cola. Guarda las tareas
  que esperan, las que están en curso y el histórico de las terminadas
  (`queue.keep_days` en `fleet.yaml`, 30 días por defecto).

Otra carpeta: `FM_DATA` en `.env` o `run_fm.py --data <dir>`.

Para mirar la cola a mano: `sqlite3 data/fleet.sqlite "SELECT order_id,
status, serial, waiting FROM tasks ORDER BY id DESC LIMIT 20"`. Con el FM
parado, borrar el fichero vacía la cola y el histórico.
