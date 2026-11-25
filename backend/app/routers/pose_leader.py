"""자세 리더 — 6자유도 자세 소스(PIKA Sense 등)로 팔로워를 조종한다 (feature/pika.md §5).

| 경로 | 하는 일 |
|---|---|
| `GET /api/leader/pose/sources` | 지금 있는 자세 소스(`piper.pose.*`) — 나이·추적·벌림·두 번 쥐기, 누가 쓰나 |
| `GET /api/leader/pose/status` | 도는 세션(팔로워마다) + 필터 파라미터 |
| `POST /api/leader/pose/start` | `{source, follower}` — 가상 리더 + 릴레이. **해제 상태로** 시작한다(팔은 그 자리) |
| `POST /api/leader/pose/engage` · `/disengage` | 정합(클러치) — 손잡이 두 번 쥐기와 같다 |
| `POST /api/leader/pose/stop` | 그 팔로워의 세션. 팔로워를 안 주면 전부 |
| `POST /api/leader/pose/params` | OneEuro `min_cutoff`·`beta`, 위치 배율 `scale` — 도는 세션에도 바로 |
"""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.pose_leader import list_sources, pose_leaders

router = APIRouter(prefix="/api/leader/pose", tags=["pose-leader"])


class StartRequest(BaseModel):
    model_config = {"extra": "forbid"}

    source: str
    follower: str


class Target(BaseModel):
    model_config = {"extra": "forbid"}

    follower: str | None = None


class ParamsRequest(BaseModel):
    model_config = {"extra": "forbid"}

    min_cutoff: float | None = None
    beta: float | None = None
    scale: float | None = None


@router.get("/sources")
async def sources():
    return {"sources": await asyncio.to_thread(list_sources)}


@router.get("/status")
async def status():
    return await asyncio.to_thread(pose_leaders.status)


@router.post("/start")
async def start(body: StartRequest):
    """릴레이가 팔을 움직이므로 다른 수동 조작·녹화·추론과 배타다. 양팔은 팔로워마다 하나씩 — 같은 팔은
    `teleop_session` 이 가린다(릴레이와 같은 규칙)."""
    from app.services import exclusivity as ex

    ex.require_idle(ex.Activity.TELEOP, allow_self=True)
    try:
        return await asyncio.to_thread(pose_leaders.start, body.source.strip(), body.follower.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


def _one(follower: str | None):
    live = pose_leaders.sessions()
    if follower:
        return pose_leaders.get(follower)
    if len(live) == 1:
        return live[0]
    raise RuntimeError("자세로 조종 중인 팔이 없습니다" if not live else "여러 팔을 조종 중입니다 — 팔로워를 정하세요")


@router.post("/engage")
async def engage(body: Target | None = None):
    try:
        s = _one(body.follower if body else None)
        await asyncio.to_thread(s.engage)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return s.status()


@router.post("/disengage")
async def disengage(body: Target | None = None):
    try:
        s = _one(body.follower if body else None)
        await asyncio.to_thread(s.disengage)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return s.status()


@router.post("/stop")
async def stop(body: Target | None = None):
    """⚠ 화면 하나가 자기 팔만 닫을 때는 팔로워를 준다 — 비우면 양팔의 다른 쪽까지 닫힌다."""
    stopped = await asyncio.to_thread(pose_leaders.stop, body.follower if body else None)
    return {"status": "stopped", "stopped": stopped}


@router.post("/params")
async def params(body: ParamsRequest):
    try:
        return {"params": pose_leaders.set_params(**body.model_dump())}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
