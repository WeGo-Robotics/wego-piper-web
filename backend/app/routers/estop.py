"""E-stop API.

감시는 **독립 프로세스** `daemons/estopd.py` 가 한다. 여기는 heartbeat 를 버스에 올리고
상태를 읽어줄 뿐이다 — 게이트웨이 이벤트 루프가 막혀도 팔이 서야 하기 때문이다.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.estop_bridge import estop_bridge

router = APIRouter(prefix="/api/estop", tags=["estop"])


class HeartbeatInfo(BaseModel):
    """브라우저가 스스로 잰 정황. **전부 선택이다.**

    ⚠ 이게 필수가 되면 안 된다 — heartbeat 는 안전 경로라, 진단용 필드 때문에
    422 로 거절되면 그 순간 팔이 선다.
    """

    gap: float | None = None      # 직전 tick 이후 브라우저가 잰 ms
    hidden: bool | None = None    # 탭이 백그라운드였나
    rtt: float | None = None      # 직전 요청의 왕복 ms (브라우저 큐 대기 포함)
    seq: int | None = None        # 보낸 순번. 빠진 번호 = 못 간 요청
    rttSeq: int | None = None     # 위 `rtt` 가 **어느 순번**의 왕복인지


@router.post("/heartbeat")
async def heartbeat(info: HeartbeatInfo | None = None):
    """브라우저 생존 신호. estopd 가 이 시각을 보고 타임아웃을 판정한다."""
    estop_bridge.heartbeat(info.model_dump() if info else None)
    return {"status": "ok"}


@router.post("/trigger")
async def trigger():
    """수동 E-stop."""
    stopped = await estop_bridge.trigger_manual()
    return {"status": "stopped", "stopped": stopped}


@router.get("/status")
async def status():
    return estop_bridge.status()
