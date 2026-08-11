"""E-stop API.

감시는 **독립 프로세스** `daemons/estopd.py` 가 한다. 여기는 heartbeat 를 버스에 올리고
상태를 읽어줄 뿐이다 — 게이트웨이 이벤트 루프가 막혀도 팔이 서야 하기 때문이다.
"""

from fastapi import APIRouter

from app.services.estop_bridge import estop_bridge

router = APIRouter(prefix="/api/estop", tags=["estop"])


@router.post("/heartbeat")
async def heartbeat():
    """브라우저 생존 신호. estopd 가 이 시각을 보고 타임아웃을 판정한다."""
    estop_bridge.heartbeat()
    return {"status": "ok"}


@router.post("/trigger")
async def trigger():
    """수동 E-stop."""
    stopped = await estop_bridge.trigger_manual()
    return {"status": "stopped", "stopped": stopped}


@router.get("/status")
async def status():
    return estop_bridge.status()
