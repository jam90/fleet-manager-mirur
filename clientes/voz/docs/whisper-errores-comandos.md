# Errores de Whisper en comandos: traer y dejar

Casos de **comando**, sin la palabra de activación, en los que Whisper no transcribe bien y un
modelo pequeño podría recuperar la orden. Sirven como benchmark de corrección.

- **Origen**: `real` = salió así en las pruebas del 2026-10-26 (whisper.cpp, `language=es`,
  `prompt=tonto`). `sint.` = inventado pero plausible (errores fonéticos típicos en castellano).
- **Esperado**: la frase corregida y la misión de `missions.json`. `mission_2` = `coger`
  ("trae", "traer"…) y `mission_1` = `dejar` ("deja", "dejar"…).
- **Hoy**: qué da el emparejado actual (palabra exacta) sin ningún modelo.

## Traer → `mission_2` (coger)

| # | Whisper | Esperado | Origen | Tipo de error | Hoy |
|---|---|---|---|---|---|
| T1 | `traes a caja` | trae la caja | real | -s añadida + artículo perdido | ✗ |
| T2 | `traes el diferencial.` | trae el diferencial | real | -s añadida | ✗ |
| T3 | `¡Pra esa caja!` | trae esa caja | real | consonante cambiada (tr → pr) | ✗ |
| T4 | `tray la caja` | trae la caja | sint. | grafía fonética | ✗ |
| T5 | `tra e la pieza` | trae la pieza | sint. | palabra partida | ✗ |
| T6 | `trai la caja` | trae la caja | sint. | vocal cambiada | ✗ |
| T7 | `tre la caja` | trae la caja | sint. | vocal perdida | ✗ |
| T8 | `traen la pieza` | trae la pieza | sint. | -n añadida | ✗ |
| T9 | `dra esa caja` | trae esa caja | sint. | consonante cambiada (t → d) | ✗ |
| T10 | `trae la` (sola) | tráela | sint. | clítico separado | ✓ (casualidad: "trae") |
| T11 | `trayela aquí` | tráela aquí | sint. | grafía fonética | ✗ |
| T12 | `trae melo` | tráemelo | sint. | clítico separado | ✓ (casualidad: "trae") |
| T13 | `traime la caja` | tráeme la caja | sint. | vocal cambiada + clítico | ✗ |
| T14 | `traed la pieza` | traer la pieza | sint. | -r → -d | ✗ |
| T15 | `cae la caja` | trae la caja | sint. | inicio perdido (tr → c) | ✗ |

## Dejar → `mission_1` (dejar)

| # | Whisper | Esperado | Origen | Tipo de error | Hoy |
|---|---|---|---|---|---|
| D1 | `de hacha caja.` | deja la caja | real | palabra partida + artículo absorbido | ✗ |
| D2 | `de ja la caja` | deja la caja | sint. | palabra partida | ✗ |
| D3 | `deje la caja` | deja la caja | sint. | vocal cambiada | ✗ |
| D4 | `dejas la pieza` | deja la pieza | sint. | -s añadida | ✗ |
| D5 | `teja la caja` | deja la caja | sint. | consonante cambiada (d → t) | ✗ |
| D6 | `decha la caja` | deja la caja | sint. | consonante cambiada (j → ch) | ✗ |
| D7 | `dela caja` | deja la caja | sint. | sílaba perdida | ✗ |
| D8 | `de jala ahí` | déjala ahí | sint. | palabra partida | ✗ |
| D9 | `de jalo hay` | déjalo ahí | sint. | palabra partida + homófono (ahí → hay) | ✗ |
| D10 | `deja la` (sola) | déjala | sint. | clítico separado | ✓ (casualidad: "deja") |
| D11 | `dejad la caja` | dejar la caja | sint. | -r → -d | ✗ |
| D12 | `de jar la pieza` | dejar la pieza | sint. | palabra partida | ✗ |
| D13 | `dejá la caja` | deja la caja | sint. | acento voseo | ✓ (normaliza tildes) |
| D14 | `lleja la caja` | deja la caja | sint. | consonante cambiada (d → ll) | ✗ |
| D15 | `hecha la caja` | deja la caja | sint. | inicio perdido | ✗ |

## Notas para el benchmark

- **Qué debe devolver el modelo**: la frase corregida, la misión (`mission_1`, `mission_2` o
  ninguna) y, a ser posible, una confianza para poder rechazar casos dudosos.
- **Métricas**:
  - aciertos de misión en estas 30 frases
  - latencia añadida por frase (se suma a la de Whisper)
  - **errores cruzados**: traer leído como dejar, o al revés. Mandan al robot a hacer lo contrario, así que son el fallo más grave.
- **Casos ambiguos a vigilar**: T15 "cae" y D15 "hecha" son palabras reales. Un modelo
  demasiado agresivo podría "corregir" frases normales.
- **El emparejado actual** acierta 4 de 30 (T10, T12, D10, D13), y solo por casualidad.
  Esta es la línea base a batir.
