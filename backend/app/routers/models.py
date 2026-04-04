import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from app.core.cli_mapping import build_inference_args
from app.services.model_scanner import scan_models, get_model, delete_model
from app.services.process_manager import process_manager
from app.services.robot_manager import robot_manager
from app.services.camera_manager import camera_manager

router = APIRouter(prefix="/api/models", tags=["models"])


def _get_first_ready_follower_port() -> str | None:
    """등록된 첫 번째 follower의 iface를 반환."""
    for arm in robot_manager.arms.values():
        if arm.ready and arm.role == "follower":
            return arm.iface
    return None


def _release_all_cameras() -> None:
    """추론 시작 전 camera_manager가 점유한 카메라를 모두 해제."""
    for cam in camera_manager.cameras.values():
        if cam.connected:
            logger.info("Releasing camera %s for inference", cam.id)
            cam.disconnect()


def _build_cameras_json(camera_mapping: dict[str, str]) -> dict:
    """카메라 매핑을 wrapper --cameras JSON으로 변환.
    {"top": "/dev/video0"} → {"top": {"type": "opencv", "index_or_path": "/dev/video0"}}
    """
    if not camera_mapping:
        return {}
    cameras = {}
    for cam_name, cam_id in camera_mapping.items():
        # /dev/videoN → 정수 인덱스
        idx: int | str = cam_id
        if isinstance(cam_id, str) and cam_id.startswith("/dev/video"):
            try:
                idx = int(cam_id.replace("/dev/video", ""))
            except ValueError:
                idx = cam_id
        cameras[cam_name] = {"type": "opencv", "index_or_path": idx}
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
    camera_mapping: dict[str, str] = {}  # {"top": "/dev/video0", "hand": "/dev/video1"}
    params: dict = {}


class InferencePreviewRequest(BaseModel):
    checkpoint_path: str
    robot_type: str | None = None
    robot_port: str | None = None
    camera_mapping: dict[str, str] = {}
    params: dict = {}


@router.post("/inference/preview")
async def preview_inference_args(body: InferencePreviewRequest):
    """추론 CLI 인자 미리보기 (실행하지 않음)."""
    robot_type = body.robot_type or robot_manager.selected_type or "piper_follower"
    robot_port = body.robot_port or _get_first_ready_follower_port()
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
    return {"args": args, "command": " ".join(args)}


class InferenceStartCustomRequest(BaseModel):
    args: list[str]  # 직접 편집된 CLI 인자 리스트


@router.post("/inference/start")
async def start_inference(body: InferenceStartRequest):
    robot_type = body.robot_type or robot_manager.selected_type
    if not robot_type:
        raise HTTPException(400, "로봇이 선택되지 않았습니다. 로봇 페이지에서 먼저 선택하세요.")
    robot_port = body.robot_port or _get_first_ready_follower_port()
    if not robot_port:
        raise HTTPException(400, "등록된 follower가 없습니다. 로봇 페이지에서 먼저 등록하세요.")
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
    return {"status": "started", "pid": process_manager.pid, "args": args}


@router.post("/inference/start-custom")
async def start_inference_custom(body: InferenceStartCustomRequest):
    """직접 편집한 CLI 인자로 추론 시작."""
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
