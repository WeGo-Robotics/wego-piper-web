import asyncio
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
    activity, cameras, datasets, debug_logs, devices, encoder, estop, eval_log,
    health, hub, inference, logs, models, params, phase, policies, policy_server,
    presets, recording, robots, training, ws,
)

# 라우터 목록 — 등록 누락을 구조적으로 막는다.
# 이전에는 import 줄과 `include_router()` 17줄을 **둘 다** 고쳐야 했고,
# 등록을 빠뜨리면 라우트가 조용히 404 가 됐다 (refactor/07-router-registration.md).
#
# `pkgutil` 자동 순회는 일부러 쓰지 않는다 — 등록 순서가 암묵적이 되고 import 부작용이 숨는다.
ROUTERS = [
    health, ws, estop, params, models, datasets, hub, inference, eval_log,
    robots, cameras, logs, debug_logs, training, recording, policy_server,
    encoder, activity, policies, presets, phase, devices,
]
from app.services.estop_bridge import estop_bridge
from app.services.param_bridge import param_bridge
from app.services.robot_manager import robot_manager
from app.services.camera_manager import camera_manager

logger = logging.getLogger(__name__)


# 장치가 사라졌는지 보는 주기. E-stop heartbeat(2초 타임아웃)보다 느슨해도 된다 —
# 이건 안전 경로가 아니라 **알림**이다. 안전 정지는 estopd 가 따로 한다.
_DEVICE_WATCH_S = 2.0


async def _watch_devices() -> None:
    """전이가 있을 때만 방송한다. 실패해도 게이트웨이를 죽이지 않는다."""
    from app.routers.ws import broadcast_device_alert
    from app.services.device_watch import device_watch

    while True:
        try:
            await asyncio.sleep(_DEVICE_WATCH_S)
            added, cleared = await asyncio.to_thread(device_watch.check)
            if not added and not cleared:
                continue
            for a in added:
                logger.warning("장치 경보: %s", a.text)
            for a in cleared:
                logger.info("장치 복구: %s (%s)", a.name, a.ident)
            await broadcast_device_alert(added, cleared)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("장치 감시 실패: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 버스 주소를 환경에 심는다. `piper_bus` 는 `PIPER_REDIS_URL` 만 보므로
    # 여기서 한 번 맞춰두면 게이트웨이·wrapper·estopd 가 같은 곳을 본다.
    if settings.redis_url:
        os.environ["PIPER_REDIS_URL"] = settings.redis_url
    # E-stop 감시는 독립 프로세스(daemons/estopd.py)가 한다.
    # 게이트웨이는 heartbeat 와 활동 PID 만 버스에 올린다 —
    # 이벤트 루프가 막혀도 팔이 서야 하기 때문이다.
    # ⚠ **게이트웨이는 세그먼트를 지우지 않는다.** 소유자는 데몬(camerad/rsd)이고,
    # 게이트웨이가 지우면 **발행 중인 파일**을 unlink 해서 발행자는 계속 쓰는데
    # 소비자는 못 여는 상태가 된다 (실제로 그랬다).
    # 고아 세그먼트는 각 데몬이 기동할 때 자기 것을 치운다.
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
    # 장치 사라짐 감시. **주기적으로 장치를 열거하지 않는다** — `/dev/shm` 을
    # 훑어 발행이 끊겼는지만 본다(세그먼트 = 임대권). RPC 도 안 타므로 2초 주기가 싸다.
    watch_task = asyncio.create_task(_watch_devices())
    yield
    watch_task.cancel()
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
