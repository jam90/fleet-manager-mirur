import httpx

from app.config import settings


class FMError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def _request(base_url: str, method: str, path: str, **kwargs) -> dict:
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=settings.fm_wait + 10) as client:
            resp = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise FMError(502, f"fleet manager request failed: {exc}") from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise FMError(resp.status_code, f"fleet manager: {detail}")
    return resp.json()


async def send_order(base_url: str, action_type: str, params: dict, order_id: str) -> dict:
    """Send a fleet order (the FM picks the robot) and wait for its decision."""
    body = {"actionType": action_type, "params": params, "orderId": order_id}
    return await _request(base_url, "POST", "/api/fleet/order", params={"wait": settings.fm_wait}, json=body)


async def get_order(base_url: str, order_id: str) -> dict:
    return await _request(base_url, "GET", f"/api/orders/{order_id}")


async def instant_action(base_url: str, serial: str, action_type: str) -> dict:
    """Send an instant action (startPause, stopPause, cancelOrder, stateRequest) to one robot."""
    return await _request(base_url, "POST", f"/api/robots/{serial}/instant", json={"actionType": action_type})
