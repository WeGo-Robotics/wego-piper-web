"""장치 경보 — 지금 무엇이 사라져 있나.

WS `device_alert` 는 **전이에서만** 온다. 나중에 페이지를 연 사람은 그 순간을
놓쳤으므로 현재 목록을 여기서 받는다 (`job_list` 와 같은 형태의 보완이다).
"""

from fastapi import APIRouter

from app.services.device_watch import device_watch

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("/alerts")
async def list_alerts():
    return {"alerts": device_watch.alerts()}
