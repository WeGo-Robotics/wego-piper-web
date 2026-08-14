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


@router.get("/summary")
async def device_summary():
    """상태바가 5초마다 읽는다 — **어느 페이지에 있든** 장치 상태가 보이게.

    싸다. 장치를 안 건드리고, 세지도 않는다: 2초마다 도는 감시가 이미 내려놓은
    `connected` 를 그대로 읽는 in-memory boolean 뿐이다 (`DeviceWatch.summary`).

    경보 목록(`/alerts`)과 **같은 판정에서 나온다.** 갈라지면 상태바는 초록인데
    경보는 떠 있는, 아무도 안 믿게 되는 화면이 된다.
    """
    return device_watch.summary()
