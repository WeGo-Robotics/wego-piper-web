"""데이터셋 레코딩 API."""

import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.cli_mapping import build_record_args
from app.core.hf_layout import repo_id_error
from app.core.config import settings
from app.services.exclusivity import Activity, require_idle
from app.services.record_manager import record_manager

router = APIRouter(prefix="/api/recording", tags=["recording"])
logger = logging.getLogger(__name__)


class RecordStartRequest(BaseModel):
    robot_type: str = "piper_follower"
    robot_port: str = ""
    robot_cameras: dict = {}
    teleop_type: str = "piper_leader"
    teleop_port: str = ""
    repo_id: str = ""
    single_task: str = ""
    num_episodes: int = 50
    fps: int = 15
    episode_time_s: int = 60
    reset_time_s: int = 60
    streaming_encoding: bool = True
    vcodec: str = "auto"
    encoder_threads: int = 4
    encoder_queue_maxsize: int = 100
    push_to_hub: bool = True
    private: bool = False
    resume: bool = False
    web_preview: bool = True  # 녹화 중 웹 카메라 미리보기 (log_rerun_data 탭)


class RecordPreviewRequest(BaseModel):
    robot_type: str = "piper_follower"
    robot_port: str = ""
    robot_cameras: dict = {}
    teleop_type: str = "piper_leader"
    teleop_port: str = ""
    repo_id: str = ""
    single_task: str = ""
    num_episodes: int = 50
    fps: int = 15
    episode_time_s: int = 60
    reset_time_s: int = 60
    streaming_encoding: bool = True
    vcodec: str = "auto"
    encoder_threads: int = 4
    encoder_queue_maxsize: int = 100
    push_to_hub: bool = True
    resume: bool = False


@router.post("/start")
async def start_recording(body: RecordStartRequest):
    """녹화 시작."""
    require_idle(Activity.RECORDING)
    # LeRobot 은 `repo_id.split("/")` 를 2개로 언패킹한다 — 슬래시가 없으면
    # 팔·카메라를 다 잡은 뒤 ValueError 로 죽어서 원인을 알기 어렵다. 시작 전에 막는다.
    if err := repo_id_error(body.repo_id):
        raise HTTPException(400, err)
    if not body.single_task:
        raise HTTPException(400, "Task 설명이 필요합니다.")
    if not body.robot_port:
        raise HTTPException(400, "Follower 포트가 필요합니다.")
    if not body.teleop_port:
        raise HTTPException(400, "Leader 포트가 필요합니다.")

    # 녹화 전 웹이 점유한 카메라 해제 (OpenCV + RealSense 둘 다).
    # 해제하지 않으면 LeRobot subprocess가 같은 USB 디바이스를 또 열어
    # 대역폭/디바이스 경합으로 녹화 루프가 목표 FPS 이하로 떨어진다.
    import time
    from app.services.camera_manager import camera_manager
    from app.services.realsense_manager import realsense_hub
    released = False
    for cam in camera_manager.cameras.values():
        if cam.connected:
            logger.info("Releasing camera %s for recording", cam.id)
            cam.disconnect()
            released = True
    if realsense_hub.release_all():
        logger.info("Released RealSense streams for recording")
        released = True
    if released:
        time.sleep(0.5)

    from app.services.control_bridge import control_bridge
    from app.services.preview_bridge import preview_bridge

    params = body.model_dump()
    params.pop("web_preview", None)

    # 헤드리스 에피소드 제어 채널은 미리보기와 무관하게 항상 켠다.
    # 버스 주소는 ProcessManager 가 모든 자식에게 넣으므로 여기서 넘기지 않는다.
    env_extra: dict[str, str] = {}
    control_bridge.start()

    # 웹 미리보기: display_data=true 로 log_rerun_data 호출을 켜고, wrapper 가
    # 그 프레임을 JPEG 로 버스에 올리도록 켠다.
    if body.web_preview:
        params["display_data"] = True
        env_extra["PIPER_PREVIEW"] = "1"
        preview_bridge.start()

    args = build_record_args(params)

    try:
        await record_manager.start(args, total_episodes=body.num_episodes, env_extra=env_extra)
    except Exception as e:
        control_bridge.stop()
        preview_bridge.stop()
        raise HTTPException(500, f"녹화 시작 실패: {e}")
    return {"status": "started", "pid": record_manager.pm.pid, "args": args}


# `escape` 를 보낸 뒤 LeRobot 이 스스로 끝날 때까지 기다리는 시간.
#
# ⚠ **2초는 너무 짧았다.** `escape` 는 "지금 에피소드를 마무리하고 끝내라"는 뜻이라,
# LeRobot 은 프레임을 데이터셋에 쓰고 **비디오를 인코딩**한 뒤에야 종료한다.
# 60초 에피소드면 카메라당 900프레임이라 2초 안에 못 끝낸다 — 그 상태로 SIGTERM 을
# 보내면 인코딩 도중에 끊긴다. 실측에서도 `escape` 후 종료까지 7초가 걸렸고,
# 그 사이(2초 시점)에 SIGTERM 이 들어갔다.
GRACEFUL_STOP_S = 60


@router.post("/stop")
async def stop_recording():
    """녹화 정지. `escape` 로 정상 종료를 요청하고, 안 끝나면 프로세스를 내린다."""
    import asyncio

    record_manager.send_key("escape")

    # 스스로 끝나면 그 즉시 빠져나온다 — 다 기다리지 않는다.
    deadline = GRACEFUL_STOP_S * 4
    for _ in range(deadline):
        if not record_manager.is_running:
            break
        await asyncio.sleep(0.25)

    graceful = not record_manager.is_running
    if not graceful:
        logger.warning(
            "escape 후 %d초 안에 끝나지 않아 프로세스를 종료합니다", GRACEFUL_STOP_S
        )
        await record_manager.stop()

    from app.services.control_bridge import control_bridge
    from app.services.preview_bridge import preview_bridge
    control_bridge.stop()
    preview_bridge.stop()
    return {"status": "stopped", "graceful": graceful}


class TaskRequest(BaseModel):
    task: str


@router.post("/task")
async def set_task(body: TaskRequest):
    """녹화 중 task 문구 변경. **다음 에피소드부터** 적용된다.

    LeRobot 은 에피소드 시작 시점의 task 를 그 에피소드의 모든 프레임에 찍는다.
    진행 중인 에피소드를 도중에 바꾸면 한 에피소드 안에서 프레임마다 task 가 달라져
    "에피소드 = 하나의 task" 전제가 깨지므로, 경계에서만 바꾼다.
    """
    task = body.task.strip()
    if not task:
        raise HTTPException(400, "task 가 비어 있습니다")
    from app.services.control_bridge import control_bridge
    if not control_bridge.set_task(task):
        raise HTTPException(409, "녹화 중이 아니거나 버스에 연결되지 않았습니다")
    return {"status": "ok", "task": task, "applies_from": "next_episode"}


@router.get("/preview")
async def list_preview_cameras():
    """녹화 중 미리보기 가능한 카메라 이름 목록 (최근 프레임이 있는 것만)."""
    from app.services.preview_bridge import preview_bridge
    return {"cameras": preview_bridge.names()}


@router.get("/preview/{name}")
async def get_preview_frame(name: str):
    """녹화 중 카메라 최신 프레임 (단일 JPEG)."""
    from fastapi.responses import Response
    from app.services.preview_bridge import preview_bridge
    data = preview_bridge.get(name)
    if data is None:
        raise HTTPException(404, "Preview unavailable")
    return Response(content=data, media_type="image/jpeg")


@router.post("/skip")
async def skip_episode():
    """이번 에피소드를 지금 마감하고 **저장**한 뒤 다음으로 (→ 키).

    ⚠ 건너뛰기가 아니다 — LeRobot 이 `save_episode()` 로 떨어진다.
    리셋 대기 중이면 "리셋 끝, 다음 시작"이 된다. 버리려면 `/rerecord` 를 쓴다.
    """
    if not record_manager.is_running:
        raise HTTPException(400, "녹화가 실행 중이 아닙니다.")
    record_manager.send_key("right")
    return {"status": "skipped"}


@router.post("/rerecord")
async def rerecord_episode():
    """현재 에피소드 재녹화 (← 키 주입)."""
    if not record_manager.is_running:
        raise HTTPException(400, "녹화가 실행 중이 아닙니다.")
    record_manager.send_key("left")
    return {"status": "rerecording"}


@router.get("/status")
async def recording_status():
    """녹화 상태."""
    return record_manager.get_status()


@router.get("/check-dataset/{repo_id:path}")
async def check_dataset_exists(repo_id: str):
    """데이터셋 로컬 존재 여부 확인."""
    dataset_path = settings.lerobot_dir / repo_id
    exists = dataset_path.exists()
    size_mb = 0.0
    if exists:
        size_mb = round(sum(f.stat().st_size for f in dataset_path.rglob("*") if f.is_file()) / (1024 * 1024), 1)
    return {"exists": exists, "path": str(dataset_path), "size_mb": size_mb}


@router.delete("/delete-dataset/{repo_id:path}")
async def delete_dataset_for_recording(repo_id: str):
    """레코딩용 데이터셋 삭제."""
    import shutil
    dataset_path = settings.lerobot_dir / repo_id
    if not dataset_path.exists():
        raise HTTPException(404, "데이터셋이 없습니다.")
    shutil.rmtree(dataset_path)
    logger.info("Deleted dataset for re-recording: %s", dataset_path)
    return {"status": "deleted", "path": str(dataset_path)}


@router.post("/preview")
async def preview_record_args(body: RecordPreviewRequest):
    """녹화 CLI 인자 미리보기."""
    params = body.model_dump()
    args = build_record_args(params)
    return {"args": args, "command": " ".join(args)}
