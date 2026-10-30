# API REST del Fleet Manager

La referencia vive en el propio FM: [`docs/api-rest.md`](../../../docs/api-rest.md).

Esta pasarela usa `POST /api/fleet/order?wait=N`, `GET /api/orders/{orderId}` y
`POST /api/robots/{serial}/instant` (ver `app/fm_client.py`).
