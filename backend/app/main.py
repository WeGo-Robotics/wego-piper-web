import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings


# heartbeat 등 빈번한 요청을 access log에서 제외
class _QuietAccessFilter(logging.Filter):
    _QUIET_PATHS = {"/api/estop/heartbeat", "/health"}

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._QUIET_PATHS)


logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())
# ⚠ 이 import 는 위 로깅 필터 설정 **뒤에** 있어야 한다 —
# uvicorn.access 필터가 라우터 import 보다 먼저 붙어야 한다.
from app.routers import (
    activity, cameras, datasets, debug_logs, encoder, estop, eval_log, health,
    hub, inference, logs, models, params, phase, policies, policy_server, presets,
    recording, robots, training, ws,
)

# 라우터 목록 — 등록 누락을 구조적으로 막는다.
# 이전에는 import 줄과 `include_router()` 17줄을 **둘 다** 고쳐야 했고,
# 등록을 빠뜨리면 라우트가 조용히 404 가 됐다 (refactor/07-router-registration.md).
#
# `pkgutil` 자동 순회는 일부러 쓰지 않는다 — 등록 순서가 암묵적이 되고 import 부작용이 숨는다.
ROUTERS = [
    health, ws, estop, params, models, datasets, hub, inference, eval_log,
    robots, cameras, logs, debug_logs, training, recording, policy_server,
    encoder, activity, policies, presets, phase,
]
from app.services.estop_bridge import estop_bridge
from app.services.param_bridge import param_bridge
from app.services.robot_manager import robot_manager
from app.services.camera_manager import camera_manager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 버스 주소를 환경에 심는다. `piper_bus` 는 `PIPER_REDIS_URL` 만 보므로
    # 여기서 한 번 맞춰두면 게이트웨이·wrapper·estopd 가 같은 곳을 본다.
    if settings.redis_url:
        os.environ["PIPER_REDIS_URL"] = settings.redis_url
    # E-stop 감시는 독립 프로세스(daemons/estopd.py)가 한다.
    # 게이트웨이는 heartbeat 와 활동 PID 만 버스에 올린다 —
    # 이벤트 루프가 막혀도 팔이 서야 하기 때문이다.
    # ⚠ 프로세스가 죽으면 `/dev/shm` 세그먼트는 그대로 남는다. 치우지 않으면
    # 소비자가 옛 세그먼트를 열어 **멈춘 화면**을 본다 — 파일이 있으니 연결은 되는데
    # 발행자가 없어 프레임이 안 온다.
    try:
        from app.services.shm_publisher import sweep_stale_segments

        sweep_stale_segments()
    except Exception as e:
        logger.warning("shm 세그먼트 정리 실패: %s", e)
    estop_bridge.connect()
    estop_bridge.sync_activities()
    await param_bridge.connect()
    # 프리셋 이관 (이전 형식 → presets/robot/). 한 번만 동작한다.
    try:
        robot_manager.migrate_legacy_presets()
    except Exception as e:
        logger.warning("Preset migration failed: %s", e)
    # 이전 세션 복원 (로봇 + 카메라)
    try:
        robot_manager.restore_session()
    except Exception as e:
        logger.warning("Robot session restore failed: %s", e)
    try:
        camera_manager.restore_session()
    except Exception as e:
        logger.warning("Camera session restore failed: %s", e)
    # 학습 프로세스 복원
    try:
        from app.services.training import train_manager
        train_manager.restore_running_process()
    except Exception as e:
        logger.warning("Train process restore failed: %s", e)
    yield
    await param_bridge.close()



app = FastAPI(title="Piper Web", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for _router_module in ROUTERS:
    app.include_router(_router_module.router)
