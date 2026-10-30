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
        raise FMError(502, f"fallo en la petición al Fleet Manager: {exc}") from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise FMError(resp.status_code, f"Fleet Manager: {detail}")
    return resp.json()


async def send_order(base_url: str, action_type: str, params: dict, order_id: str) -> dict:
    """Envía una order a la flota (el FM elige el robot) y espera su decisión."""
    body = {"actionType": action_type, "params": params, "orderId": order_id}
    return await _request(base_url, "POST", "/api/fleet/order", params={"wait": settings.fm_wait}, json=body)


async def get_order(base_url: str, order_id: str) -> dict:
    return await _request(base_url, "GET", f"/api/orders/{order_id}")


async def cancel_order(base_url: str, order_id: str) -> dict:
    """Cancela una order de la flota: si espera en la cola del FM sale de ella, y si
    ya la hace un robot es su cancelOrder. Espera la respuesta del FM."""
    return await _request(base_url, "POST", f"/api/orders/{order_id}/cancel", params={"wait": settings.fm_wait})


async def instant_action(base_url: str, serial: str, action_type: str) -> dict:
    """Envía una instantAction (startPause, stopPause, cancelOrder, stateRequest) a un robot."""
    return await _request(base_url, "POST", f"/api/robots/{serial}/instant", json={"actionType": action_type})
