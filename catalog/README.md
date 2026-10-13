# Catálogo de la flota

Lo que el FM lleva a los robots (sessions, mapas, positions y, más adelante,
missions), con **GUIDs comunes en toda la flota**. Es configuración, como
`config/fleet.yaml`: se versiona en git y no es estado de ejecución. Diseño
completo en `docs/plan-admin.md` (§3.1 catálogo, §4 formato del GUID).

```
catalog/
├── sessions/<guid>.json     site del MiR
├── maps/<guid>.json         metadatos del mapa (resolución, origen, session)
├── maps/<guid>.png          su imagen (base_map), aparte para poder abrirla
├── positions/<guid>.json    una por position, con su docking offset si lo tiene
└── missions/                (pendiente)
```

- **Un fichero por recurso, con su GUID como nombre.** El GUID ya dice qué
  es y de quién cuelga: `imperialfleet-position00-01-0001-017` es la
  position 17 del mapa 1 de la instalación 01.
- Los campos son los del REST del MiR, con las referencias (`map_id`,
  `session_id`, `parent_id`) ya traducidas a GUIDs del catálogo.
- `_origen` dice de qué robot, recurso y fecha se capturó. No se manda al
  robot; sirve para adoptar los recursos existentes (GUID antiguo → nuevo).
- `creada_por_mir: true` (entrada de marcador o cargador): el MiR la crea
  solo, con su propio GUID, al crear la madre. Nuestro GUID solo nombra el
  fichero; en el robot se busca por `parent_id`. El docking offset de un
  marcador va dentro de su fichero, sin GUID, por lo mismo.
- Los ficheros los escribe el FM (hoy, `scripts/catalog_capture.py`; se
  llevan al robot con `scripts/catalog_deploy.py`).
  Editarlos a mano es posible, pero un GUID no se cambia nunca.
