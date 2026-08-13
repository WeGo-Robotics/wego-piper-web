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

# 정지 시점에 사이드카를 쓰려면 시작 때의 매핑이 필요하다.
# 재시작하면 비지만, 그때는 녹화도 없으므로 남길 것도 없다.
_last_recording: dict = {}

router = APIRouter(prefix="/api/recording", tags=["recording"])
logger = logging.getLogger(__name__)


class RecordStartRequest(BaseModel):
    robot_type: str = "piper_follower"
    robot_port: str = ""
    robot_cameras: dict = {}
    # `{카메라키: 장치id}`. 주면 백엔드가 `--robot.cameras` JSON 을 조립한다.
    # 프론트가 조립하면 백엔드 설정(`camera_transport`)을 몰라 **녹화만 옛 경로**를 탄다.
    camera_mapping: dict[str, str] = {}
    # 카메라 요청 해상도·fps (`direct` 에서만 쓰인다 — `shm` 은 발행자가 정한다)
    camera_width: int = 0
    camera_height: int = 0
    camera_fps: int = 0
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
    camera_mapping: dict[str, str] = {}
    camera_width: int = 0
    camera_height: int = 0
    camera_fps: int = 0
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

    # 전송 방식에 맞게 카메라 준비. `direct` 는 해제하고 `shm` 은 붙잡는다 —
    # 두 방식이 정반대라 한 곳(`camera_config`)에서 판단한다.
    from app.services.camera_config import (
        CameraPrepareError,
        build_cameras_json,
        check_camera_config,
        prepare_cameras,
    )

    # 낡은 프론트가 보낸 장치 설정을 그대로 넘기면 데몬이 쥔 장치를 또 열려다
    # LeRobot 3겹 안쪽에서 `Device or resource busy` 로 죽는다.
    if err := check_camera_config(body.robot_cameras):
        raise HTTPException(400, err)

    cam_w = body.camera_width or 0
    cam_h = body.camera_height or 0
    cam_fps = body.camera_fps or body.fps

    # ⚠ 요청 프로파일을 **여기서** 넘겨야 장치에 반영된다. 안 넘기면 데몬이
    # 기본값으로 열고(D405 는 848x480@10), 녹화 루프가 그 10Hz 에 묶인다.
    try:
        prepare_cameras(body.camera_mapping, purpose="recording",
                        width=cam_w, height=cam_h, fps=cam_fps)
    except CameraPrepareError as e:
        raise HTTPException(400, str(e))

    # 팔도 같은 순서로 준비한다. `shm` 에서는 게이트웨이가 CAN 을 쥔 채로
    # 상태를 발행해야 프록시 드라이버가 붙는다.
    from app.services.robot_config import ArmPrepareError, prepare_arms

    try:
        prepare_arms([body.robot_port], purpose="recording")
    except ArmPrepareError as e:
        raise HTTPException(400, str(e))

    from app.services.control_bridge import control_bridge
    from app.services.preview_bridge import preview_bridge

    params = body.model_dump()
    params.pop("web_preview", None)
    mapping = params.pop("camera_mapping", None) or {}
    params.pop("camera_width", None)
    params.pop("camera_height", None)
    params.pop("camera_fps", None)
    if mapping:
        params["robot_cameras"] = build_cameras_json(
            mapping, width=cam_w, height=cam_h, fps=cam_fps
        )

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

    # ⚠ 정지 시점에는 매핑이 없다 — 지금 붙잡아 둔다. 사이드카는 데이터셋이
    # 만들어진 **뒤**에야 쓸 수 있어서 시작 때 바로 못 남긴다.
    _last_recording["repo_id"] = body.repo_id
    _last_recording["camera_mapping"] = dict(body.camera_mapping or {})

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

    # 카메라 해석에 필요한 값을 데이터셋 옆에 남긴다. LeRobot 은 `meta/info.json` 에
    # 카메라 설정을 안 적어서, 깊이 인코딩 범위 같은 건 여기 없으면 영영 모른다.
    from app.services.camera_config import write_camera_sidecar

    repo_id = _last_recording.get("repo_id") or ""
    mapping = _last_recording.get("camera_mapping") or {}
    if repo_id and mapping:
        write_camera_sidecar(settings.lerobot_dir / repo_id, mapping)

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
    """녹화 CLI 인자 미리보기. 시작과 **같은 조립기**를 써야 미리보기가 거짓말을 안 한다."""
    from app.services.camera_config import build_cameras_json

    params = body.model_dump()
    mapping = params.pop("camera_mapping", None) or {}
    cam_w = params.pop("camera_width", 0)
    cam_h = params.pop("camera_height", 0)
    cam_fps = params.pop("camera_fps", 0) or body.fps
    if mapping:
        params["robot_cameras"] = build_cameras_json(
            mapping, width=cam_w, height=cam_h, fps=cam_fps
        )
    args = build_record_args(params)
    return {"args": args, "command": " ".join(args)}
