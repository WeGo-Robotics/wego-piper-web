import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from app.core.cli_mapping import build_inference_args, build_grpc_client_args
from app.core.config import settings
from app.services.model_scanner import scan_models, get_model, delete_model
from app.services.process_manager import process_manager
from app.services.robot_manager import robot_manager
from app.services.camera_manager import camera_manager
from app.services.train_manager import train_manager

router = APIRouter(prefix="/api/models", tags=["models"])


# ── 모델 검색 경로 관리 ──

@router.get("/paths")
async def list_model_paths():
    return [{"path": str(p), "exists": p.exists()} for p in settings.model_paths]


class PathRequest(BaseModel):
    path: str


@router.post("/paths")
async def add_model_path(body: PathRequest):
    from pathlib import Path
    p = Path(body.path).expanduser().resolve()
    if not p.exists():
        raise HTTPException(400, f"경로가 존재하지 않습니다: {p}")
    if not p.is_dir():
        raise HTTPException(400, f"디렉토리가 아닙니다: {p}")
    paths = settings.add_model_path(str(p))
    return {"paths": paths}


@router.post("/paths/remove")
async def remove_model_path(body: PathRequest):
    paths = settings.remove_model_path(body.path)
    return {"paths": paths}


def _get_first_ready_follower_port() -> str | None:
    """등록된 첫 번째 follower의 iface를 반환."""
    for arm in robot_manager.arms.values():
        if arm.ready and arm.role == "follower":
            return arm.iface
    return None


def _release_all_cameras() -> None:
    """추론 시작 전 camera_manager가 점유한 카메라를 모두 해제."""
    released = False
    for cam in camera_manager.cameras.values():
        if cam.connected:
            logger.info("Releasing camera %s for inference", cam.id)
            cam.disconnect()
            released = True
    if released:
        import time
        time.sleep(0.5)  # 커널이 디바이스를 해제할 시간 확보


def _build_cameras_draccus(camera_mapping: dict[str, str]) -> str:
    """카메라 매핑을 draccus 형식 문자열로 변환 (robot_client.py용).
    {"top": "/dev/video12"} → "{ top: {type: opencv, index_or_path: '/dev/video12', width: 640, height: 480, fps: 30}}"
    """
    if not camera_mapping:
        return ""
    parts = []
    for cam_name, cam_id in camera_mapping.items():
        parts.append(f"{cam_name}: {{type: opencv, index_or_path: '{cam_id}', width: 640, height: 480, fps: 30}}")
    return "{ " + ", ".join(parts) + " }"


def _build_cameras_json(camera_mapping: dict[str, str]) -> dict:
    """카메라 매핑을 wrapper --cameras JSON으로 변환.
    {"top": "/dev/video0"} → {"top": {"type": "opencv", "index_or_path": "/dev/video0"}}
    """
    if not camera_mapping:
        return {}
    cameras = {}
    for cam_name, cam_id in camera_mapping.items():
        if not cam_id:
            continue
        cameras[cam_name] = {"type": "opencv", "index_or_path": cam_id, "backend": 200}
    return cameras


@router.get("")
async def list_models():
    return scan_models()


@router.get("/{model_id:path}")
async def model_detail(model_id: str):
    model = get_model(model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    return model


@router.delete("/{model_id:path}")
async def remove_model(model_id: str):
    if not delete_model(model_id):
        raise HTTPException(404, "Model not found")
    return {"status": "deleted"}


class InferenceStartRequest(BaseModel):
    checkpoint_path: str
    robot_type: str | None = None
    robot_port: str | None = None
    robot_ports: list[str] = []  # bimanual: [left_port, right_port]
    camera_mapping: dict[str, str] = {}
    params: dict = {}
    inference_mode: str = "local"  # "local" | "server"
    server_address: str = "127.0.0.1:8088"
    policy_type: str = "act"
    actions_per_chunk: int = 100
    aggregate_fn: str = "weighted_average"
    offset_correction: bool = False
    smoothing: str = "none"
    smoothing_window: int = 5


class InferencePreviewRequest(BaseModel):
    checkpoint_path: str
    robot_type: str | None = None
    robot_port: str | None = None
    robot_ports: list[str] = []
    camera_mapping: dict[str, str] = {}
    inference_mode: str = "local"
    server_address: str = "127.0.0.1:8088"
    policy_type: str = "act"
    aggregate_fn: str = "weighted_average"
    offset_correction: bool = False
    smoothing: str = "none"
    smoothing_window: int = 5
    actions_per_chunk: int = 100
    params: dict = {}


@router.post("/inference/preview")
async def preview_inference_args(body: InferencePreviewRequest):
    """추론 CLI 인자 미리보기 (실행하지 않음)."""
    robot_type = body.robot_type or robot_manager.selected_type or "piper_follower"
    robot_port = body.robot_port or _get_first_ready_follower_port()
    cameras = _build_cameras_json(body.camera_mapping)

    if body.inference_mode == "server":
        # gRPC wrapper 모드
        cameras = _build_cameras_json(body.camera_mapping)
        build_params = {
            "server_address": body.server_address,
            "robot_type": robot_type,
            "robot_port": robot_port,
            "checkpoint_path": body.checkpoint_path,
            "policy_type": body.policy_type,
            "policy_device": "cuda",
            "actions_per_chunk": body.actions_per_chunk,
            "chunk_size_threshold": 0.8,
            "aggregate_fn": body.aggregate_fn,
            "offset_correction": body.offset_correction,
            "smoothing": body.smoothing,
            "smoothing_window": body.smoothing_window,
            "task": body.params.get("task", "do the task"),
            "fps": 20,
        }
        if cameras:
            build_params["cameras"] = cameras
        if len(body.robot_ports) >= 2:
            build_params["robot_ports"] = body.robot_ports
        args = build_grpc_client_args(build_params)
    else:
        # 로컬 wrapper 모드
        build_params = {
            "checkpoint_path": body.checkpoint_path,
            "robot_type": robot_type,
            "robot_port": robot_port,
            "device": "cuda",
            "use_amp": True,
            **body.params,
        }
        if cameras:
            build_params["cameras"] = cameras
        args = build_inference_args(build_params)

    import shlex
    return {"args": args, "command": " ".join(shlex.quote(a) for a in args)}


class InferenceStartCustomRequest(BaseModel):
    args: list[str]  # 직접 편집된 CLI 인자 리스트


def _clear_viz_cache():
    """이전 추론의 시각화 이미지 삭제."""
    import glob, os
    for f in glob.glob("/dev/shm/piper_viz_*.jpg") + glob.glob("/tmp/piper_viz_*.jpg"):
        try: os.remove(f)
        except OSError: pass


@router.post("/inference/start")
async def start_inference(body: InferenceStartRequest):
    _clear_viz_cache()
    if train_manager.is_running:
        raise HTTPException(409, "학습이 실행 중입니다. 학습을 먼저 중지하세요.")
    robot_type = body.robot_type or robot_manager.selected_type
    if not robot_type:
        raise HTTPException(400, "로봇이 선택되지 않았습니다. 로봇 페이지에서 먼저 선택하세요.")
    is_bimanual = len(body.robot_ports) >= 2
    robot_port = body.robot_port or _get_first_ready_follower_port()
    if not is_bimanual and not robot_port:
        raise HTTPException(400, "등록된 follower가 없습니다. 로봇 페이지에서 먼저 등록하세요.")

    if body.inference_mode == "server":
        # gRPC wrapper 모드
        cameras = _build_cameras_json(body.camera_mapping)
        build_params = {
            "server_address": body.server_address,
            "robot_type": robot_type,
            "robot_port": robot_port if not is_bimanual else body.robot_ports[0],
            "checkpoint_path": body.checkpoint_path,
            "policy_type": body.policy_type,
            "policy_device": "cuda",
            "actions_per_chunk": body.actions_per_chunk,
            "chunk_size_threshold": 0.8,
            "aggregate_fn": body.aggregate_fn,
            "offset_correction": body.offset_correction,
            "smoothing": body.smoothing,
            "smoothing_window": body.smoothing_window,
            "task": body.params.get("task", "do the task"),
            "fps": 20,
        }
        if cameras:
            build_params["cameras"] = cameras
        if is_bimanual:
            build_params["robot_ports"] = body.robot_ports
        args = build_grpc_client_args(build_params)
    else:
        # 로컬 wrapper 모드
        cameras = _build_cameras_json(body.camera_mapping)
        build_params = {
            "checkpoint_path": body.checkpoint_path,
            "robot_type": robot_type,
            "robot_port": robot_port,
            "device": "cuda",
            "use_amp": True,
            **body.params,
        }
        if cameras:
            build_params["cameras"] = cameras
        args = build_inference_args(build_params)

    _release_all_cameras()

    try:
        await process_manager.start(args)
    except Exception as e:
        raise HTTPException(500, f"프로세스 시작 실패: {e}")
    return {"status": "started", "pid": process_manager.pid, "args": args, "mode": body.inference_mode}


@router.post("/inference/start-custom")
async def start_inference_custom(body: InferenceStartCustomRequest):
    """직접 편집한 CLI 인자로 추론 시작."""
    _clear_viz_cache()
    if not body.args:
        raise HTTPException(400, "CLI 인자가 비어있습니다")

    # 추론 전 연결된 모든 카메라 해제 (wrapper가 카메라를 직접 열므로)
    _release_all_cameras()

    try:
        await process_manager.start(body.args)
    except Exception as e:
        raise HTTPException(500, f"프로세스 시작 실패: {e}")
    return {"status": "started", "pid": process_manager.pid, "args": body.args}


@router.post("/inference/stop")
async def stop_inference():
    await process_manager.stop()
    return {"status": "stopped"}
