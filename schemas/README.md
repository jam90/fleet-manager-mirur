# JSON Schemas VDA 5050 v3.0.0

Copiados de `https://github.com/VDA5050/VDA5050`, tag `3.0.0`, carpeta
`json_schemas/` (fecha de descarga: 2026-09-16). Renombrados a `*.schema.json`.

Parches locales (los originales no son JSON válido o son inconsistentes):

- `order.schema.json`: eliminada una coma final tras la propiedad `weight`
  (línea 314 del original).
- `factsheet.schema.json`: eliminadas comas finales; en
  `typeSpecification.required` se sustituye `mobileRobotKinematic` por
  `mobileRobotKinematics` (nombre real de la propiedad y del PDF §7.10).

`state`, `connection` e `instantActions` están sin modificar.
