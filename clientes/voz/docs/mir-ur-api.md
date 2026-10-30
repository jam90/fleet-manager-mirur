# API REST del Fleet Manager

La referencia vive en el propio FM: [`docs/api-rest.md`](../../../docs/api-rest.md).

Esta pasarela usa `POST /api/fleet/order?wait=N`, `GET /api/orders/{orderId}` y
`POST /api/orders/{orderId}/cancel?wait=N` (ver `app/fm_client.py`). Para parar no hace
falta saber el robot: el FM cancela la order también si aún espera en su cola.
