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
    activity, alignment, cameras, cloud, datasets, debug_logs, devices, encoder, estop,
    eval_log, external, health, hub, inference, logs, models, orchestrator, params, phase,
    policies, policy_server, presets, recording, robots, system, training, vision,
    ws, yolo_train,
    web_leader, sim_scenes, sim_assets, sim_demo,
)

# 라우터 목록 — 등록 누락을 구조적으로 막는다.
# 이전에는 import 줄과 `include_router()` 17줄을 **둘 다** 고쳐야 했고,
# 등록을 빠뜨리면 라우트가 조용히 404 가 됐다 (refactor/07-router-registration.md).
#
# `pkgutil` 자동 순회는 일부러 쓰지 않는다 — 등록 순서가 암묵적이 되고 import 부작용이 숨는다.
ROUTERS = [
    health, ws, estop, params, models, datasets, hub, inference, eval_log,
    robots, cameras, logs, debug_logs, training, recording, policy_server, system,
    encoder, activity, policies, presets, phase, devices, vision, yolo_train,
    orchestrator, external, alignment, cloud,
    web_leader, sim_scenes, sim_assets, sim_demo,
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
    from app.routers.ws import broadcast_device_alert, broadcast_load_alert
    from app.services.device_watch import device_watch
    from app.services.light_watch import light_watch
    from app.services.load_alerts import load_alert_watch

    while True:
        try:
            await asyncio.sleep(_DEVICE_WATCH_S)
            # 조명 샘플링을 **판정보다 먼저** — check() 의 _collect 가 이번 주기
            # 측정의 경보를 가져간다. 프레임 읽기(memcpy)와 64×64 평균이라 싸다.
            try:
                await asyncio.to_thread(light_watch.sample)
            except Exception as exc:
                logger.debug("조명 감시 실패: %s", exc)
            # 관절 과부하는 **사건**이라 장치 경보와 따로 나간다 — 조건이
            # 풀렸다고 지워지면 안 된다 (`load_alerts` 머리말).
            try:
                overloads = await asyncio.to_thread(load_alert_watch.check)
            except Exception as exc:
                logger.debug("부하 경보 확인 실패: %s", exc)
                overloads = []
            if overloads:
                for a in overloads:
                    logger.warning("관절 과부하: %s", a["text"])
                await broadcast_load_alert(overloads)
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

    # ⚠ **깔린 데몬이 이번 릴리스와 맞는지 기동 때 한 번 본다** (릴리스 규칙 R4).
    #   판정이 버전 카드에만 있으면 화면을 연 사람만 안다 — 2026-09-15 NUC 은 wheel 이
    #   최신인데 데몬 소스가 9월 1일자라 회색 카드가 죽었고, 아무 데서도 그 말을 안 했다.
    #   여기서 못 고치지만(고칠 곳은 호스트의 `./piper-install.sh` 다) **말은 한다.**
    try:
        from app.services import version as _version

        _stale = _version.staleness()
        if _stale["wheels"]:
            logger.warning("데몬 wheel 이 이번 릴리스와 다릅니다: %s — 설정 → 서비스에서 재시작하세요",
                           ", ".join(_stale["wheels"]))
        if _stale["daemons"]:
            logger.warning("데몬 소스가 적용된 릴리스와 다릅니다 (%s) — 받아 둔 번들에서 "
                           "./piper-install.sh 를 한 번 돌리면 따라잡습니다", _stale["daemons"])
        if _stale["ok"]:
            logger.info("데몬 버전 확인: 이번 릴리스와 맞습니다")
    except Exception as e:
        logger.debug("데몬 버전 확인 실패: %s", e)
    # 가상환경 — 적용해 둔 가상환경이 simd 에 아직 올라 있나 (feature/sim-scene-editor.md §6).
    # ⚠ simd 는 명세를 **메모리에만** 들고 있어 재시작하면 기본 가상환경으로 돌아온다. 그걸
    #   아무도 안 보면 사람은 자기 세계가 올라가 있다고 믿은 채 엉뚱한 가상환경에서 수집한다.
    try:
        from app.services import sim_scenes as _scenes

        if _scenes.ensure_applied():
            logger.info("가상환경을 다시 올렸습니다")
    except Exception as e:
        logger.debug("가상환경 확인 실패: %s", e)
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
    # 정책 서버·작업 유닛 재부착 — 유닛은 사는데 게이트웨이만 idle 로 알면
    # activity 에서 빠져 배타 모드 가드가 헛돈다 (재시작 후 실측)
    try:
        from app.services.policy_server_manager import policy_server_manager
        if policy_server_manager.restore_running_process():
            logger.info("Policy server reattached: %s", policy_server_manager.address)
    except Exception as e:
        logger.warning("Policy server restore failed: %s", e)
    # ⚠ 녹화도 유닛이다. 상태만 잃으면 화면은 "녹화 안 함" 인데 팔은 계속
    #   움직이고, 배타 가드가 헛돌아 그 위에 학습·추론을 얹을 수 있게 된다.
    try:
        from app.services.record_manager import record_manager
        if record_manager.restore_running_process():
            logger.info("Recording reattached (pid=%s)", record_manager.pm.pid)
    except Exception as e:
        logger.warning("Recording restore failed: %s", e)
    try:
        from app.services import dataset_jobs
        restored_jobs = dataset_jobs.restore_running_jobs()
        if restored_jobs:
            logger.info("Dataset jobs reattached: %s", ", ".join(restored_jobs))
    except Exception as e:
        logger.warning("Dataset job restore failed: %s", e)
    # 장치 사라짐 감시. **주기적으로 장치를 열거하지 않는다** — `/dev/shm` 을
    # 훑어 발행이 끊겼는지만 본다(세그먼트 = 임대권). RPC 도 안 타므로 2초 주기가 싸다.
    watch_task = asyncio.create_task(_watch_devices())
    # 자원 추이 샘플러 — 대시보드가 로딩 때 지난 15분을 받아 가려면
    # 아무도 안 보는 동안에도 서버가 쌓고 있어야 한다.
    from app.services import trends
    trend_task = asyncio.create_task(trends.run_sampler())
    # 고아 GPU 스캐너 — **빌린 기계가 관리 밖에서 도는지** 주기적으로 본다(§6-3).
    # ⚠ 화면을 열면 인스턴스 탭이 보여 주지만, 고아가 생기는 상황이 곧 아무도 화면을
    #   안 보는 상황이다(게이트웨이가 죽었다 살아난 직후). 그때 요금은 계속 나간다.
    # ⚠ **자동으로 파기하지 않는다** — 다른 기계의 게이트웨이가 돌리는 학습일 수 있다.
    from app.services.cloud import sweeper

    # ⚠ **재기동했다면 관리 중인 임대는 하나도 없다.** 임대 태스크는 asyncio 태스크라
    #   프로세스와 함께 죽는다 — 학습과 달리 재부착 경로가 없다. 그런데 레코드의
    #   `instance_id` 는 Redis 에 남아서, 안 비우면 스캐너가 "관리 중" 으로 읽고
    #   **조용해진다**. 스캐너가 있어야 할 바로 그 경우에 그렇다.
    try:
        _freed = sweeper.release_claims()
        if _freed:
            logger.warning(
                "재기동 전에 빌려 둔 기계가 있습니다 — **지금 아무도 관리하지 않습니다**: "
                "%s. 클라우드 GPU → 인스턴스에서 확인하고 파기하세요",
                ", ".join(str(i) for i in _freed))
    except Exception as e:
        logger.warning("임대 주장 정리 실패: %s", e)
    orphan_task = asyncio.create_task(sweeper.run_sweeper())
    yield
    orphan_task.cancel()
    trend_task.cancel()
    watch_task.cancel()
    await param_bridge.close()



#: Swagger UI 머리말. `/docs` 를 여는 사람이 **가장 먼저 알아야 할 것**만 적는다 —
#: 나머지는 각 라우터의 docstring 이 태그별로 채운다.
API_DESCRIPTION = """\
LeRobot 원격 제어 게이트웨이의 REST/WebSocket 표면.

* **`/api/*`** — 웹 UI 가 쓰는 내부 API. LAN 신뢰를 전제로 인증이 없다.
* **`/api/ext/v1/*`** — 외부 시스템용 계약. `PIPER_API_TOKEN` Bearer 필수이며
  미설정이면 전체가 503 이다. 깨는 변경은 v2 를 만든다 (`feature/external-api.md`).
* **`/ws`** — 로그·프로세스 상태 스트리밍. Swagger 로는 호출할 수 없다.

⚠ **여기서 호출하면 실기가 움직인다.** 이 문서는 읽기 전용 참조가 아니라 살아 있는
게이트웨이다. 추론·녹화·텔레옵을 시작하는 경로는 heartbeat 의무를 함께 지며
(`POST /api/estop/heartbeat`, 2.5초 타임아웃), Swagger UI 는 heartbeat 를 보내지
않으므로 estopd 가 곧 SIGKILL 한다. 시작 계열은 화면이나 외부 클라이언트로 부르고,
`/docs` 는 조회·점검 위주로 쓴다."""


def _api_version() -> str:
    """도는 버전을 제목 옆에 박는다. 못 찾아도 앱을 못 띄우면 안 된다."""
    try:
        from app.services.version import running_version

        return running_version().get("version") or "unknown"
    except Exception:  # pragma: no cover - 버전 조회 실패가 기동을 막지 않는다
        return "unknown"


# docs_url/openapi_url 은 기본값 그대로 (`/docs`, `/openapi.json`) — 리버스 프록시
# (`frontend/nginx.conf`) 와 vite 프록시가 이 경로를 넘겨야 배포본에서도 열린다.
app = FastAPI(
    title="Piper Studio",
    version=_api_version(),
    description=API_DESCRIPTION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for _router_module in ROUTERS:
    app.include_router(_router_module.router)
