"""서비스 상태와 재시작 — "고쳤는데 왜 안 되지"를 없앤다.

유닛은 **기동 시점의 코드로 돈다.** 이 저장소는 그걸로 두 번 크게 헤맸다:
rsd 가 이틀 전 코드로 돌아 고친 버그가 재현됐고, 게이트웨이가 새 라우트를
모른 채로 404 만 돌려줬다. 화면이 그 사실을 말해주면 둘 다 몇 초짜리 일이다.
"""

import asyncio
import logging
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import units
from app.services.exclusivity import Activity, require_idle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/services")
async def list_services():
    """유닛 목록 + 게이트웨이 자신. `stale` 이 이 응답의 요점이다."""
    return {"units": [u.to_dict() for u in units.list_units()],
            "gateway": units.gateway_status()}


class RestartRequest(BaseModel):
    name: str


@router.post("/services/restart")
async def restart_service(body: RestartRequest):
    """유닛 재시작.

    ⚠ **추론·녹화 중에는 막는다.** rsd 를 재시작하면 카메라 스트림이 끊기고,
    robotd 면 팔 상태 발행이 멈춘다 — 돌고 있는 에피소드가 그대로 깨진다.
    """
    require_idle(Activity.CAMERA_ACCESS)
    ok, msg = units.restart_unit(body.name)
    if not ok:
        raise HTTPException(400, msg)
    return {"status": "restarted", "name": body.name}


@router.post("/restart")
async def restart_gateway():
    """게이트웨이(이 프로세스)를 다시 띄운다.

    ⚠ **응답을 보낸 뒤에** 자신을 갈아탄다. 먼저 죽으면 브라우저는 "요청 실패"만
    보고, 재시작이 된 건지 터진 건지 알 수 없다.

    ⚠ 감독자가 없으면 되살려 줄 사람도 없다 — 그래서 `execv` 로 **같은 명령줄을
    그대로** 다시 실행한다. 새로 뜨는 것이 아니라 이 프로세스가 갈아입는 것이다.
    """
    require_idle(Activity.CAMERA_ACCESS)
    argv = units.respawn_argv()
    if not argv:
        raise HTTPException(400, "이 방식으로는 스스로 재시작할 수 없습니다")

    async def _respawn():
        # 응답이 소켓을 빠져나갈 틈을 준다. 짧게 — 사용자는 기다리고 있다.
        await asyncio.sleep(0.5)
        logger.warning("게이트웨이 재시작: execv %s", argv)
        try:
            os.execv(argv[0], argv)
        except Exception as exc:               # pragma: no cover - 되돌아올 수 없다
            logger.error("재시작 실패 — 이 프로세스는 그대로 돕니다: %s", exc)

    asyncio.create_task(_respawn())
    return {"status": "restarting", "pid": os.getpid()}
