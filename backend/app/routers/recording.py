"""데이터셋 레코딩 API."""

import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.cli_mapping import build_record_args
from app.services.record_manager import record_manager
from app.services.process_manager import process_manager
from app.services.train_manager import train_manager

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
    if process_manager.state.value not in ("idle", "error"):
        raise HTTPException(409, "추론이 실행 중입니다.")
    if train_manager.is_running:
        raise HTTPException(409, "학습이 실행 중입니다.")
    if record_manager.is_running:
        raise HTTPException(409, "녹화가 이미 실행 중입니다.")
    if not body.repo_id:
        raise HTTPException(400, "데이터셋 이름(repo_id)이 필요합니다.")
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

    params = body.model_dump()
    args = build_record_args(params)

    try:
        await record_manager.start(args, total_episodes=body.num_episodes)
    except Exception as e:
        raise HTTPException(500, f"녹화 시작 실패: {e}")
    return {"status": "started", "pid": record_manager.pm.pid, "args": args}


@router.post("/stop")
async def stop_recording():
    """녹화 정지 (ESC 키 주입 후 프로세스 종료)."""
    record_manager.send_key("escape")
    import asyncio
    await asyncio.sleep(2)
    if record_manager.is_running:
        await record_manager.stop()
    return {"status": "stopped"}


@router.post("/skip")
async def skip_episode():
    """현재 에피소드 건너뛰기 (→ 키 주입)."""
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
    from pathlib import Path
    dataset_path = Path.home() / ".cache" / "huggingface" / "lerobot" / repo_id
    exists = dataset_path.exists()
    size_mb = 0.0
    if exists:
        size_mb = round(sum(f.stat().st_size for f in dataset_path.rglob("*") if f.is_file()) / (1024 * 1024), 1)
    return {"exists": exists, "path": str(dataset_path), "size_mb": size_mb}


@router.delete("/delete-dataset/{repo_id:path}")
async def delete_dataset_for_recording(repo_id: str):
    """레코딩용 데이터셋 삭제."""
    import shutil
    from pathlib import Path
    dataset_path = Path.home() / ".cache" / "huggingface" / "lerobot" / repo_id
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
