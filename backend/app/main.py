import logging
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
from app.routers import health, ws, estop, params, models, datasets, hub, inference, eval_log, robots, cameras, logs, training, recording, policy_server, debug_logs, encoder, activity, policies, presets
from app.services.estop_bridge import estop_bridge
from app.services.zmq_bridge import zmq_bridge
from app.services.robot_manager import robot_manager
from app.services.camera_manager import camera_manager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # E-stop 감시는 독립 프로세스(daemons/estopd.py)가 한다.
    # 게이트웨이는 heartbeat 와 활동 PID 만 버스에 올린다 —
    # 이벤트 루프가 막혀도 팔이 서야 하기 때문이다.
    estop_bridge.connect()
    estop_bridge.sync_activities()
    await zmq_bridge.connect()
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
    await zmq_bridge.close()



app = FastAPI(title="Piper Web", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(ws.router)
app.include_router(estop.router)
app.include_router(params.router)
app.include_router(models.router)
app.include_router(datasets.router)
app.include_router(hub.router)
app.include_router(inference.router)
app.include_router(eval_log.router)
app.include_router(robots.router)
app.include_router(cameras.router)
app.include_router(logs.router)
app.include_router(debug_logs.router)
app.include_router(training.router)
app.include_router(recording.router)
app.include_router(policy_server.router)
app.include_router(encoder.router)
app.include_router(activity.router)
app.include_router(policies.router)
app.include_router(presets.router)
