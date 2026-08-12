import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from app.core.cli_mapping import build_inference_args, build_grpc_client_args
from app.core.config import settings
from app.services.exclusivity import Activity, require_idle
from app.services.camera_config import (
    CameraPrepareError,
    build_cameras_json,
    prepare_cameras,
    release_all_cameras,
)
from app.services.model_scanner import scan_models, get_model, delete_model
from app.services.param_bridge import param_bridge
from app.services.process_manager import process_manager
from app.services.robot_manager import robot_manager
from app.services.camera_manager import camera_manager

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


def _build_args_for(body, robot_type: str, robot_port: str | None) -> list[str]:
    """추론 CLI 인자 생성 — `/inference/preview` 와 `/inference/start` 가 함께 쓴다.

    두 곳에 복붙되어 있던 것을 합쳤다. 미리보기가 실제 실행과 다르면 사용자가
    화면에서 확인한 명령이 거짓이 된다.

    `body.params`(UI 슬라이더 값)는 두 모드 모두 그대로 넘긴다 — gRPC 모드는
    이전에 `task` 만 꺼내 쓰고 `fps` 를 20 으로 하드코딩해서 슬라이더 값이 전부 유실됐다.
    실제로 실릴 키는 `cli_mapping.OVERRIDE_KEYS` 와 각 ARGS_MAP 이 정한다.
    """
    cameras = build_cameras_json(body.camera_mapping)
    is_bimanual = len(body.robot_ports) >= 2

    if body.inference_mode == "server":
        params = {
            "server_address": body.server_address,
            "robot_type": robot_type,
            "robot_port": body.robot_ports[0] if is_bimanual else robot_port,
            "checkpoint_path": body.checkpoint_path,
            "policy_type": body.policy_type,
            "policy_device": "cuda",
            "actions_per_chunk": body.actions_per_chunk,
            "chunk_size_threshold": 0.8,
            "aggregate_fn": body.aggregate_fn,
            "offset_correction": body.offset_correction,
            "smoothing": body.smoothing,
            "smoothing_window": body.smoothing_window,
            "debug": body.debug_mode,
            **body.params,
            "task": body.params.get("task", "do the task"),
        }
        if cameras:
            params["cameras"] = cameras
        if is_bimanual:
            params["robot_ports"] = body.robot_ports
        return build_grpc_client_args(params)

    params = {
        "checkpoint_path": body.checkpoint_path,
        "robot_type": robot_type,
        "robot_port": robot_port,
        "device": "cuda",
        "use_amp": True,
        "debug": body.debug_mode,
        **body.params,
    }
    if cameras:
        params["cameras"] = cameras
    return build_inference_args(params)


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
    debug_mode: bool = False


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
    debug_mode: bool = False


@router.post("/inference/preview")
async def preview_inference_args(body: InferencePreviewRequest):
    """추론 CLI 인자 미리보기 (실행하지 않음)."""
    robot_type = body.robot_type or robot_manager.selected_type or "piper_follower"
    robot_port = body.robot_port or _get_first_ready_follower_port()
    args = _build_args_for(body, robot_type, robot_port)

    import shlex
    return {"args": args, "command": " ".join(shlex.quote(a) for a in args)}


class InferenceStartCustomRequest(BaseModel):
    args: list[str]  # 직접 편집된 CLI 인자 리스트


@router.post("/inference/start")
async def start_inference(body: InferenceStartRequest):
    require_idle(Activity.INFERENCE)
    robot_type = body.robot_type or robot_manager.selected_type
    if not robot_type:
        raise HTTPException(400, "로봇이 선택되지 않았습니다. 로봇 페이지에서 먼저 선택하세요.")
    is_bimanual = len(body.robot_ports) >= 2
    robot_port = body.robot_port or _get_first_ready_follower_port()
    if not is_bimanual and not robot_port:
        raise HTTPException(400, "등록된 follower가 없습니다. 로봇 페이지에서 먼저 등록하세요.")

    args = _build_args_for(body, robot_type, robot_port)

    try:
        prepare_cameras(body.camera_mapping, purpose="inference")
    except CameraPrepareError as e:
        raise HTTPException(400, str(e))

    # 추론 기동 전 로봇팔 에러 플래그 조회 + 무조건 클리어 (subprocess가 CAN을 쥐기 전)
    follower_ifaces = body.robot_ports if is_bimanual else ([robot_port] if robot_port else None)
    _clear_arm_errors("inference-start", follower_ifaces)

    # ⚠ 지난 세션의 파라미터를 버린다. ZMQ 는 소켓을 닫으면 큐도 사라졌지만
    # Redis 리스트는 남아서, 안 비우면 이전 추론 끝에 민 슬라이더 값이
    # 새 추론 시작 직후에 적용된다 (refactor/daemon-split.md 3단계).
    await param_bridge.clear()

    env_extra = {"PIPER_DEBUG_DIR": settings.debug_dir} if body.debug_mode else None
    try:
        await process_manager.start(args, env_extra=env_extra)
    except Exception as e:
        raise HTTPException(500, f"프로세스 시작 실패: {e}")
    return {"status": "started", "pid": process_manager.pid, "args": args, "mode": body.inference_mode}


@router.post("/inference/start-custom")
async def start_inference_custom(body: InferenceStartCustomRequest):
    """직접 편집한 CLI 인자로 추론 시작."""
    # 카메라를 해제하기 전에 막는다 — 녹화 중이면 여기서 뺏으면 안 된다
    require_idle(Activity.INFERENCE)
    if not body.args:
        raise HTTPException(400, "CLI 인자가 비어있습니다")

    # 추론 전 연결된 모든 카메라 해제 (wrapper가 카메라를 직접 열므로)
    release_all_cameras("inference")

    # 추론 기동 전 로봇팔 에러 플래그 조회 + 무조건 클리어 (연결된 모든 follower)
    _clear_arm_errors("inference-start")

    await param_bridge.clear()   # 위와 같은 이유 — 세션 격리

    env_extra = {"PIPER_DEBUG_DIR": settings.debug_dir} if "--debug" in body.args else None
    try:
        await process_manager.start(body.args, env_extra=env_extra)
    except Exception as e:
        raise HTTPException(500, f"프로세스 시작 실패: {e}")
    return {"status": "started", "pid": process_manager.pid, "args": body.args}


@router.post("/inference/stop")
async def stop_inference():
    await process_manager.stop()
    # 추론 정지 후(subprocess가 CAN을 놓은 뒤) 로봇팔 에러 플래그 조회 + 무조건 클리어
    _clear_arm_errors("inference-stop")
    return {"status": "stopped"}
