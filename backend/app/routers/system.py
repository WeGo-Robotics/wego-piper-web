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


@router.get("/resources")
async def resources(since: float | None = None):
    """GPU·CPU·디스크 + 추이 견본. **대시보드 전용이라 실패해도 200 이다.**

    ⚠ `nvidia-smi` 는 드라이버가 걸리면 D-state 로 멈춘다 — D405 의 UVC 질의로
    똑같이 겪었고 그때 **이벤트 루프 전체가 먹통**이 됐다. `to_thread` 로 빼서
    루프를 막지 않고, 안에서 타임아웃을 건다. 자원 표시가 없는 것과 웹이 안 뜨는
    것은 비교할 일이 아니다.

    GPU 는 샘플러(trends)가 4초마다 뜬 마지막 견본을 재사용한다 — 같은 위험한
    호출을 폴링마다 또 하지 않기 위해서다. 샘플러가 아직 안 떴을 때만 직접 묻는다.

    `samples` 는 서버가 쌓아 둔 최근 15분 추이다. `since`(epoch 초) 없이 부르면
    창 전체가 온다 — **페이지 로딩 때 그래프가 처음부터 차 있는 이유다.**
    이후 폴링은 마지막 견본의 `t` 를 `since` 로 넘겨 새 것만 받는다.
    """
    from app.core.config import settings
    from app.services import resources as res
    from app.services import trends

    gpu_list = trends.latest_gpus() or await asyncio.to_thread(res.gpus)
    disks = [d for d in (await asyncio.to_thread(res.disk, str(settings.datasets_dir)),)
             if d]
    return {"gpus": gpu_list, "disks": disks,
            "cpu_pct": trends.latest_cpu(),
            "samples": trends.samples(since)}


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
