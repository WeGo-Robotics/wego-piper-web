from fastapi import APIRouter

from app.services.estop_watchdog import estop_watchdog

router = APIRouter(prefix="/api/estop", tags=["estop"])


@router.post("/heartbeat")
async def heartbeat():
    estop_watchdog.heartbeat()
    return {"status": "ok"}


@router.post("/trigger")
async def trigger():
    await estop_watchdog.trigger()
    return {"status": "stopped"}


@router.get("/status")
async def status():
    return {
        "active": estop_watchdog.active,
        "triggered": estop_watchdog.triggered,
    }
