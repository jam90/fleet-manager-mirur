import logging
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.config import settings
from app.fm_client import FMError, get_order, instant_action, send_order
from app.logic import decide, load_missions
from app.voice_log import log_event, new_id, save_audio
from app.whisper_client import WhisperError, is_healthy, transcribe

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger("uvicorn.error")
Fleet = Literal["sim", "real"]

app = FastAPI(title="Mission Manager")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/voice")
async def voice(audio: UploadFile):
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")
    filename = audio.filename or "audio.webm"
    content_type = audio.content_type or "application/octet-stream"
    voice_id = new_id()
    audio_file = save_audio(voice_id, data, filename, content_type)
    try:
        text = await transcribe(data, filename, content_type)
    except WhisperError as exc:
        log_event("voice", id=voice_id, audio=audio_file, error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    result = decide(text)
    logger.info("transcription=%r wake=%s mission=%s", text, result["wake"], result["mission"])
    log_event("voice", id=voice_id, audio=audio_file, whisper_prompt=settings.whisper_prompt, **result)
    return result


@app.post("/api/missions/{mission}")
async def send_mission(mission: str, fleet: Fleet = "sim"):
    missions = load_missions()
    if mission not in missions or "actionType" not in missions[mission]:
        raise HTTPException(status_code=404, detail=f"unknown mission: {mission}")
    entry = missions[mission]
    order_id = f"mm-{uuid.uuid4().hex[:8]}"
    try:
        order = await send_order(settings.fleet_url(fleet), entry["actionType"], entry["params"], order_id)
    except FMError as exc:
        log_event("order", fleet=fleet, mission=mission, orderId=order_id, error=str(exc.detail))
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    logger.info("fleet=%s mission=%s order=%s status=%s", fleet, mission, order_id, order.get("status"))
    log_event("order", fleet=fleet, mission=mission, orderId=order_id, status=order.get("status"))
    return order


@app.get("/api/orders/{order_id}")
async def order_status(order_id: str, fleet: Fleet = "sim"):
    try:
        return await get_order(settings.fleet_url(fleet), order_id)
    except FMError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@app.post("/api/orders/{order_id}/cancel")
async def cancel_order(order_id: str, fleet: Fleet = "sim"):
    base_url = settings.fleet_url(fleet)
    try:
        order = await get_order(base_url, order_id)
        if not order.get("serial"):
            raise HTTPException(status_code=409, detail=f"order {order_id} has no robot assigned")
        result = await instant_action(base_url, order["serial"], "cancelOrder")
    except FMError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    logger.info("fleet=%s cancel order=%s robot=%s", fleet, order_id, order["serial"])
    log_event("cancel", fleet=fleet, orderId=order_id, serial=order["serial"])
    return {"orderId": order_id, "serial": order["serial"], **result}


@app.get("/api/config")
async def voice_config():
    return {
        "wakeWord": settings.wake_word,
        "confirmWord": settings.confirm_word,
        "armWindowS": settings.arm_window_s,
        "confirmWindowS": settings.confirm_window_s,
        "missions": {
            name: {"actionType": m.get("actionType"), "instant": m.get("instant"), "words": sorted(m["words"])}
            for name, m in load_missions().items()
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok", "whisper": "ok" if await is_healthy() else "down"}
