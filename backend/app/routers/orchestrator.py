"""에피소드 오케스트레이터 API — 분리수거 루프 시작/정지/상태.

루프 본체는 [services/orchestrator.py](../services/orchestrator.py) —
여기는 시작 가드(배타 모드)와 표면뿐이다.
"""

import logging

from fastapi import APIRouter, HTTPException

from app.services.exclusivity import Activity, require_idle
from app.services.orchestrator import OrchestratorConfig, orchestrator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/orchestrator", tags=["orchestrator"])


@router.post("/start")
async def start(cfg: OrchestratorConfig):
    require_idle(Activity.ORCHESTRATOR)
    try:
        await orchestrator.start(cfg)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"status": "started", "max_episodes": cfg.max_episodes, "dry_run": cfg.dry_run}


@router.post("/stop")
async def stop():
    await orchestrator.stop()
    return {"status": "stopped"}


@router.get("/status")
async def status():
    return orchestrator.status()
