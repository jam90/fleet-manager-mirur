# Fleet Manager — REST API

HTTP API for sending missions to the MiR250 fleet and reading robot status.
It is served by the Fleet Manager (FM) itself, alongside its web UI.

- **Base URL:** `http://<fm-host>:8050` (simulated fleet: `:8051`)
- **Format:** JSON in, JSON out (`Content-Type: application/json`)
- **Authentication:** none (workshop LAN only)
- **Interactive docs:** `GET /api/docs` (Swagger) · spec: `GET /openapi.json`

> ⚠️ On the real fleet, `POST …/order` makes a robot **physically move**.
> Test your integration against the simulated fleet first.

---

## Quick start

```bash
# 1. Send a mission; the FM picks the robot. Wait up to 5 s for its decision.
curl -X POST 'http://fm-host:8050/api/fleet/order?wait=5' \
     -H 'Content-Type: application/json' \
     -d '{"actionType": "coger", "orderId": "mes-000123"}'

# 2. Poll until it finishes
curl http://fm-host:8050/api/orders/mes-000123

# 3. Robot status
curl http://fm-host:8050/api/robots
```

---

## Concepts

**actionType**: the name of a mission as configured in the FM (e.g. `coger`,
`dejar`, `ir_a_espera`, `abrir_puerta`). Each one maps to a mission created on
the robot. `GET /api/fleet` lists the available ones and their parameters.

**params**: mission inputs as `{key: value}`. For `position` parameters the
value is the **position name** as it appears on the robot (not a GUID).

**orderId**: identifies a mission request. You may send your own (recommended:
your system's job id), or the FM generates `ui-xxxxxxxx`. Re-sending an
`orderId` the FM has already processed is ignored (it is not executed twice),
so it is safe to retry.

**Fleet order vs. directed order**

| | Who picks the robot | Endpoint |
|---|---|---|
| Fleet order (normal case) | The FM: a free robot with enough battery; the one with most battery wins | `POST /api/fleet/order` |
| Directed order | You | `POST /api/robots/{serial}/order` |

The FM has **no queue**. If no robot can take the order right now, it is
**rejected** and your system must retry later.

### Order lifecycle (`status`)

```
PENDING ──► ASSIGNED (fleet order) ─┐
        └─► ACCEPTED (directed)  ───┴─► RUNNING ──► FINISHED
                                                └─► FAILED
PENDING ──► REJECTED
```

| status | Meaning | Final? |
|---|---|---|
| `PENDING` | Sent, the FM has not decided yet | no |
| `ASSIGNED` | Fleet order accepted; `serial` = chosen robot | no |
| `ACCEPTED` | Directed order accepted by the robot | no |
| `RUNNING` | Robot is executing the mission | no |
| `FINISHED` | Mission completed | **yes** |
| `FAILED` | Mission aborted or cancelled (see `errors`) | **yes** |
| `REJECTED` | Not executed; reason in `errorType` + `description` | **yes** |

---

## Endpoints

### Send a mission

#### `POST /api/fleet/order` — the FM picks the robot
#### `POST /api/robots/{serial}/order` — a specific robot

Query parameters:

| Name | Type | Default | Description |
|---|---|---|---|
| `wait` | number (0–30) | `0` | Seconds to wait for the FM's decision before replying |

Body:

```json
{
  "actionType": "abrir_puerta",
  "params": { "target_pos": "H2D1-VL" },
  "orderId": "mes-000124"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `actionType` | string | yes | Mission name (see `GET /api/fleet`) |
| `params` | object | no | Mission parameters `{key: value}` |
| `orderId` | string | no | Your id; generated if omitted |

Responses:

| Code | When | Body |
|---|---|---|
| `200` | Without `wait`: sent, `status: "PENDING"`. With `wait`: the FM decided in time | Order object |
| `202` | With `wait`: no decision in time (still `PENDING`); poll `GET /api/orders/{orderId}` | Order object |
| `400` | Unknown `actionType` (or not supported by that robot) | `{"detail": "…"}` |
| `404` | Unknown `serial` | `{"detail": "…"}` |
| `422` | Malformed body, or `wait` out of range | FastAPI validation error |

Example, assigned:

```json
{
  "orderId": "mes-000123", "orderUpdateId": 0,
  "target": "fleet", "actionType": "coger",
  "serial": "mir-2", "status": "ASSIGNED",
  "description": "asignado a mir-2", "errorType": null,
  "actionStates": [], "errors": [],
  "created": 1791531981.85, "updated": "2026-10-09T07:46:22.070Z"
}
```

Example, rejected (both robots busy):

```json
{
  "orderId": "mes-000125", "status": "REJECTED", "serial": null,
  "errorType": "NO_MOBILE_ROBOT_AVAILABLE",
  "description": "ningún robot puede atender la order: mir-1: ocupado; mir-2: batería 17.0% < 25%",
  "...": "…"
}
```

> A `200` means the FM **answered**, not that the order was accepted.
> Always check `status`.

### Track orders

#### `GET /api/orders/{orderId}`

Returns the order object. `404` if this FM has not seen that order. It forgets
orders when it restarts or after it has seen 200 newer ones.

**Order object**

| Field | Type | Description |
|---|---|---|
| `orderId` | string | |
| `orderUpdateId` | int | Always `0` for orders sent through this API |
| `target` | `"fleet"` \| `"robot"` \| null | How it was sent (`null` = arrived over MQTT from another client) |
| `actionType` | string \| null | |
| `serial` | string \| null | Robot executing it (`null` until assigned) |
| `status` | string | See lifecycle |
| `description` | string \| null | Assignment message or rejection reason |
| `errorType` | string \| null | Rejection code (see below) |
| `actionStates` | array | VDA 5050 `actionStates`: `{actionId, actionType, actionStatus}` |
| `errors` | array | VDA 5050 errors that reference this order |
| `created` | number | Unix time (s) when the FM first saw it |
| `updated` | string \| null | ISO 8601 UTC time of the last update |

#### `GET /api/orders?limit=50`

Most recent orders first (`limit` 1–200). Includes orders that arrived over MQTT.

### Robot status

#### `GET /api/robots`
#### `GET /api/robots/{serial}`

```json
{
  "serial": "mir-1", "manufacturer": "MiR", "connection": "ONLINE",
  "reachable": true,
  "battery": 54.9, "charging": false,
  "driving": true, "paused": false, "operatingMode": "AUTOMATIC",
  "position": { "x": 29.8, "y": 7.96, "theta": -0.014, "mapId": "…", "localized": true },
  "orderId": "mes-000123", "orderUpdateId": 0,
  "actionStates": [ { "actionId": "mes-000123-a1", "actionType": "coger", "actionStatus": "RUNNING" } ],
  "instantActionStates": [],
  "errors": [], "information": [ { "infoType": "MISSION", "infoLevel": "INFO", "infoDescriptor": "…" } ],
  "timestamp": "2026-10-09T07:46:22.070Z"
}
```

| Field | Description |
|---|---|
| `connection` | FM ↔ MQTT broker: `ONLINE` / `OFFLINE` / `CONNECTION_BROKEN` |
| `reachable` | `false` if the FM cannot reach the robot over the network |
| `battery` | State of charge, % |
| `position` | `x`, `y` in m, `theta` in rad, on map `mapId` |
| `orderId` | Order the robot is executing or last executed (`""` = none) |
| `timestamp` | When this status was produced (updated every second) |

If the FM has not published anything for that robot yet, the object only has
`serial`, `manufacturer`, `connection` and `"state": null`.

#### `GET /api/robots/{serial}/state` · `/connection` · `/factsheet`

The latest raw VDA 5050 v3 message (identical to the one on MQTT).
`503` if none has been published yet.

#### `GET /api/health`

```json
{ "ok": true,
  "robots": { "mir-1": { "connection": "ONLINE", "reachable": true, "lastState": "2026-10-09T07:46:22.070Z" },
              "mir-2": { "connection": "ONLINE", "reachable": true, "lastState": "2026-10-09T07:46:22.071Z" } } }
```

`ok` is `true` only if every robot is `ONLINE` and reachable.

### Catalog

#### `GET /api/fleet`

Robots, the missions each one accepts, and battery thresholds.

```json
{
  "robots": [
    { "serial": "mir-1", "manufacturer": "MiR", "driver": "mir", "battery_min": 25,
      "actions": [
        { "action_type": "abrir_puerta", "description": "Simular Apertura Puerta H2DX MIRUR",
          "params": [ { "key": "target_pos", "kind": "position", "required": true,
                        "choices": ["H2D1-VL", "…"] } ] }
      ] }
  ],
  "fleet_manufacturer": "imperial_fleet",
  "auto_charge": { "battery_floor": 20, "priority": 10, "abort_cooldown_s": 60 },
  "instant_actions": ["startPause", "stopPause", "cancelOrder", "stateRequest"]
}
```

- `battery_min`: a robot below this % does not accept new orders.
- `battery_floor`: below this %, the FM automatically sends the robot to charge.
- `choices`: allowed values, when the FM knows them (e.g. position names).

#### `GET /api/robots/{serial}/actions`

Just the `actions` list for one robot.

### Control a robot

#### `POST /api/robots/{serial}/instant`

```json
{ "actionType": "cancelOrder" }
```

| actionType | Effect |
|---|---|
| `startPause` | Pause the robot |
| `stopPause` | Resume |
| `cancelOrder` | Cancel the current order (its status becomes `FAILED`) |
| `stateRequest` | Publish a fresh status immediately |

Response: `{"actionId": "ui-xxxxxxxx"}`. The result shows up in the robot's
`instantActionStates`. `400` = not allowed, `404` = unknown robot.

---

## Error codes (`errorType`)

| errorType | Meaning | What to do |
|---|---|---|
| `NO_MOBILE_ROBOT_AVAILABLE` | No robot free with enough battery (details in `description`) | Retry later |
| `OTHER_ORDER_ACTIVE` | Directed order: that robot is busy with another order | Retry later or use a fleet order |
| `MOBILE_ROBOT_NOT_AVAILABLE` | Robot unreachable, or running a job started outside the FM | Check the robot |
| `NO_ROUTE_TO_TARGET` | Mission or position does not exist on that robot | Fix `actionType` / `params` |
| `VALIDATION_FAILURE` | Missing required parameter or invalid order | Fix the request |
| `ORDER_EXECUTION_FAILED` | Mission aborted or cancelled while running | Check the robot; resend if needed |

---

## Recommended client flow

1. `POST /api/fleet/order?wait=5` with your own `orderId`.
2. If `status` is `REJECTED`: log `description` and retry later.
3. Otherwise poll `GET /api/orders/{orderId}` every 1–2 s until `status` is
   `FINISHED`, `FAILED` or `REJECTED`.
4. On network errors or timeouts, resend with the **same** `orderId`. It will
   not run twice.

### Python example

```python
import time
import requests

FM = "http://fm-host:8050"
FINAL = {"FINISHED", "FAILED", "REJECTED"}


def run_mission(action_type: str, params: dict, order_id: str, timeout_s: float = 600) -> dict:
    r = requests.post(f"{FM}/api/fleet/order", params={"wait": 5}, timeout=10,
                      json={"actionType": action_type, "params": params, "orderId": order_id})
    r.raise_for_status()
    order = r.json()
    deadline = time.monotonic() + timeout_s
    while order["status"] not in FINAL and time.monotonic() < deadline:
        time.sleep(1)
        order = requests.get(f"{FM}/api/orders/{order_id}", timeout=5).json()
    return order


result = run_mission("coger", {}, "mes-000123")
print(result["status"], result["serial"], result.get("description"))
```

### JavaScript (fetch) example

```js
const FM = "http://fm-host:8050";

async function sendOrder(actionType, params = {}, orderId) {
  const r = await fetch(`${FM}/api/fleet/order?wait=5`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ actionType, params, orderId }),
  });
  if (r.status >= 400) throw new Error((await r.json()).detail);
  return r.json();             // check .status: ASSIGNED / REJECTED / PENDING
}
```

---

## Limitations

- One mission per order (one action). Chain missions from your side by
  sending the next one after `FINISHED`.
- No queue: orders are rejected when no robot is free.
- Order history is kept in memory only and is lost when the FM restarts.
- No authentication. Do not expose it outside the workshop network.
- The same operations are available over MQTT (VDA 5050 v3); see the FM README.
