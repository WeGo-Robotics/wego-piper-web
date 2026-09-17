"""스크립트 시연 — 사람 없이 에피소드를 만든다 (feature/sim-env.md 4단계).

수집은 이 시연을 **리더로** 읽는다: `teleop_port = sim_demo.LEADER_NAME`.
그래서 수집·추론·E-stop 경로가 하나도 안 바뀐다.
"""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.sim_demo import DemoError, LEADER_NAME, sim_demo

router = APIRouter(prefix="/api/sim/demo", tags=["sim-demo"])


class StartBody(BaseModel):
    episodes: int = 1
    #: 수집 중이면 **False** — 녹화 프로세스가 팔로워를 쥔다(릴레이와 둘이 못 쥔다).
    relay: bool = True
    randomize: bool = True


@router.get("/status")
async def status():
    return {**sim_demo.status(), "leader": LEADER_NAME}


@router.post("/start")
async def start(body: StartBody):
    try:
        return await asyncio.to_thread(sim_demo.start, body.episodes, body.relay, body.randomize)
    except DemoError as exc:
        raise HTTPException(400, str(exc))


@router.post("/stop")
async def stop():
    return await asyncio.to_thread(sim_demo.stop)
