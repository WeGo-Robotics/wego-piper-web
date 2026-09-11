"""웹 리더 — 조종 창의 입력 (feature/web-leader.md). 브라우저는 눌린 키·마우스 델타를
30Hz 로 보내고, 자세는 서비스가 적분한다. 팔로워는 릴레이가 움직인다."""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.web_leader import web_leader

router = APIRouter(prefix="/api/leader/web", tags=["web-leader"])


class StartRequest(BaseModel):
    follower: str
    mode: str = "joint"


class InputRequest(BaseModel):
    keys: list[str] = []
    mouse: dict = {}
    shift: bool = False
    ctrl: bool = False
    mode: str | None = None
    click: str | None = None
    toggle_lock: bool = False
    select: str | None = None


@router.post("/start")
async def start(body: StartRequest):
    """조종 시작. **녹화 중이면 발행만** — 녹화 프로세스가 팔을 움직이고 창은 입력만 보낸다.
    그 밖엔 릴레이가 움직이므로 다른 수동 조작·추론과 배타다."""
    from app.services import exclusivity as ex

    if body.mode not in ("joint", "ee"):
        raise HTTPException(400, f"모르는 모드입니다: {body.mode}")
    recording = ex.is_running(ex.Activity.RECORDING)
    if web_leader.is_running:
        return web_leader.status()            # 창을 다시 클릭한 것 — 이미 돌고 있다
    if not recording:
        ex.require_idle(ex.Activity.TELEOP)
    try:
        return await asyncio.to_thread(web_leader.start, body.follower, body.mode, not recording)
    except RuntimeError as exc:
        raise HTTPException(409 if "이미" in str(exc) else 400, str(exc))


@router.post("/input")
async def send_input(body: InputRequest):
    """30Hz 상태 전송. 실패는 409 — 세션이 없으면 창이 다시 시작한다."""
    try:
        web_leader.input(keys=body.keys, mouse=body.mouse, shift=body.shift, ctrl=body.ctrl,
                         mode=body.mode, click=body.click, toggle_lock=body.toggle_lock,
                         select=body.select)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True}


@router.post("/stop")
async def stop():
    await asyncio.to_thread(web_leader.stop)
    return {"status": "stopped"}


@router.get("/status")
async def status():
    return web_leader.status()


class ResetRequest(BaseModel):
    follower: str
    arm_only: bool = False      # T 키: 팔만 파킹, 큐브·조명 유지


class CubeRequest(BaseModel):
    cam: str = "sim:top"
    u: float
    v: float
    aspect: float = 4.0 / 3.0


@router.post("/cube")
async def move_cube(body: CubeRequest):
    """블럭 옮기기 — 탑뷰 클릭 픽셀(정규화 u,v)로 큐브를 테이블 위 그 자리로 순간이동."""
    from app.services import sim_robot_client as sim

    if not body.cam.startswith("sim:"):
        raise HTTPException(400, "시뮬 카메라에서만 블럭을 옮길 수 있습니다")
    r = await asyncio.to_thread(sim.call, "cube_from_view", body.cam, body.u, body.v, body.aspect)
    if not isinstance(r, dict):
        raise HTTPException(400, "simd 가 응답하지 않습니다")
    return r


@router.post("/reset")
async def reset_world(body: ResetRequest):
    """환경 리셋 — 큐브 시작 위치, 팔 파킹, 속도 0 (feature/web-leader.md §5).
    조종 중이면 리더 자세도 파킹으로 되돌려 팔이 도로 끌려가지 않게 한다."""
    from app.services.web_leader import reset_sim_world

    try:
        return await asyncio.to_thread(reset_sim_world, body.follower, body.arm_only)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
