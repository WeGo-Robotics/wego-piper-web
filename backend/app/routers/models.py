import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from app.core.cli_mapping import build_inference_args, build_grpc_client_args
from app.core.config import settings
from app.core.policies import takes_language
from app.services.exclusivity import Activity, require_idle
from app.services.camera_config import (
    CameraPrepareError,
    build_cameras_json,
    prepare_cameras,
    release_all_cameras,
)
from app.services.model_scanner import scan_models, get_model, delete_model
from app.services.robot_config import ArmPrepareError, prepare_arms, resolve_robot_type
from app.services.param_bridge import param_bridge
from app.services import inference_state
from app.services.process_manager import process_manager
from app.services.robot_manager import robot_manager

router = APIRouter(prefix="/api/models", tags=["models"])


# ── 모델 검색 경로 관리 ──

def _readable(p) -> bool:
    """⚠ `exists()` 는 상위 디렉토리를 못 읽으면 `PermissionError` 를 **던진다.**

    컨테이너에서 붙었던 `/root/.cache/huggingface/hub` 가 목록에 남아 있어서
    이 엔드포인트가 통째로 500 이었고, 설정 화면은 경로 목록을 **빈 채로** 보여줬다
    — 즉 문제의 그 경로를 지울 방법이 화면에 없었다. 스캐너는 이미 건너뛰고 있다.
    """
    try:
        return p.exists()
    except OSError:
        return False


@router.get("/paths")
async def list_model_paths():
    return [{"path": str(p), "exists": _readable(p)} for p in settings.model_paths]


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


def _clear_arm_errors(label: str, ifaces: list[str] | None = None) -> list[str]:
    """추론 시작/종료 시 로봇팔 에러 플래그를 조회한 뒤 무조건 클리어한다.

    실패해도 추론 흐름은 막지 않는다(best-effort). 반환은 **슬립 경고 문구** —
    0x150 리셋이 보고 프레임을 재동기화하며 드러낸 간극이다(piper_sdk #120).
    시작 응답에 실어 화면이 사람에게 알린다.

    ## 호출 순서 — `direct` 에서만 의미가 있다

    `robot_transport="direct"` 면 subprocess 가 CAN 을 직접 여므로, 시작은
    **기동 전**, 종료는 **정지 후**에 불러야 버스 경합을 피한다.

    `shm` 에서는 robotd 가 CAN 을 영구 소유하고 게이트웨이는 RPC 로만 말하므로
    **경합 자체가 없다** — 순서가 아무래도 상관없다. 두 방식이 공존하는 동안은
    `direct` 쪽 제약이 더 강하니 그쪽에 맞춰 둔다. `direct` 를 걷어내는 날
    이 문단도 같이 지운다 (refactor/robot-transport.md 5단계).
    """
    from app.services import robot_manager as robot_manager_mod

    try:
        report = robot_manager.clear_arm_errors(ifaces)
    except Exception as e:
        logger.warning("[%s] 로봇팔 에러 클리어 실패: %s", label, e)
        return []
    if not report:
        logger.info("[%s] 에러 클리어 대상 follower 없음", label)
        return []
    for r in report:
        err = r.get("error") or {}
        logger.info("[%s] %s 에러 클리어: code=0x%04X flags=%s cleared=%s",
                    label, r["iface"], err.get("err_code", 0),
                    err.get("flags", []), r["cleared"])
    return robot_manager_mod.slip_warnings(report)


def _build_args_for(body, robot_type: str, robot_port: str | None) -> list[str]:
    """추론 CLI 인자 생성 — `/inference/preview` 와 `/inference/start` 가 함께 쓴다.

    두 곳에 복붙되어 있던 것을 합쳤다. 미리보기가 실제 실행과 다르면 사용자가
    화면에서 확인한 명령이 거짓이 된다.

    `body.params`(UI 슬라이더 값)는 두 모드 모두 그대로 넘긴다 — gRPC 모드는
    이전에 `task` 만 꺼내 쓰고 `fps` 를 20 으로 하드코딩해서 슬라이더 값이 전부 유실됐다.
    실제로 실릴 키는 `cli_mapping.OVERRIDE_KEYS` 와 각 ARGS_MAP 이 정한다.
    예외는 `task` 하나 — 아래 참고.
    """
    # 전송 방식에 맞는 드라이버로 바꾼다. **여기서 바꿔야** 미리보기와 실행이 같다 —
    # 한쪽에만 걸면 화면에 보이는 명령이 실제로 도는 것과 달라진다.
    robot_type = resolve_robot_type(robot_type)
    cameras = build_cameras_json(body.camera_mapping)
    is_bimanual = len(body.robot_ports) >= 2

    # ⚠ **언어를 안 받는 정책에는 `task` 를 아예 안 싣는다.**
    # 화면은 ACT 일 때 입력란을 감추지만 값은 localStorage 에 남아 계속 실려 왔다.
    # 그래서 입력한 적 없는 옛 문장이 CLI 미리보기와 wrapper 로그에 떴다.
    # 감추는 것과 안 보내는 것은 다르고, **판정은 여기 한 곳**이다 —
    # 화면이 따로 판단하면 프리셋·CLI 직접 편집 같은 다른 경로가 새어 나간다.
    user_params = dict(body.params)
    if not takes_language(body.policy_type):
        user_params.pop("task", None)

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
            **user_params,
        }
        # gRPC 정책 서버는 `task` 키를 요구한다 — 언어를 쓰는 정책일 때만 채운다.
        if takes_language(body.policy_type):
            params["task"] = body.params.get("task", "do the task")
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
        **user_params,
    }
    if cameras:
        params["cameras"] = cameras
    if is_bimanual:
        # 양팔은 이제 로컬도 된다 — wrapper 가 robot_ports 를 보고 bi 로봇을 조립한다.
        # robot_port 는 첫 팔로 남겨 둔다 (wrapper 의 단팔 폴백 인자)
        params["robot_port"] = body.robot_ports[0]
        params["robot_ports"] = body.robot_ports
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
    # 시작 전에 1회 적용할 카메라 프로파일 — 학습 데이터와 같은 노출·색으로
    # 추론해야 관측이 같은 분포가 된다. 빈 값이면 적용하지 않는다.
    camera_profile: str = ""
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

    # ⚠ **카메라를 먼저 준비한다.** `shm` 에서는 세그먼트에서 실제 해상도를 읽어
    # 인자에 싣는데, 연결 전이면 세그먼트가 없어 기본값(640x480)으로 떨어진다 —
    # D405 처럼 848x480 인 카메라가 어긋난 채로 데이터셋 메타에 박힌다.
    try:
        prepare_cameras(body.camera_mapping, purpose="inference")
    except CameraPrepareError as e:
        raise HTTPException(400, str(e))

    # 작업 프로파일 — 학습 데이터와 다른 노출·색으로 추론하면 정책이 다른
    # 분포를 본다. 지정했으면 연결 뒤 한 번 적용하고, 없는 프로파일이면 막는다
    # (녹화 시작과 같은 규칙 — routers/recording.py).
    profile_report: dict | None = None
    if body.camera_profile:
        import asyncio

        from app.services import camera_profiles

        profile_report = await asyncio.to_thread(
            camera_profiles.apply_for_task, body.camera_profile)
        if profile_report.get("error"):
            raise HTTPException(
                400, f"카메라 프로파일 '{body.camera_profile}': {profile_report['error']}")

    follower_ifaces = body.robot_ports if is_bimanual else ([robot_port] if robot_port else None)

    # ⚠ **팔도 카메라와 같은 순서다** — 인자를 만들기 전에 준비한다.
    # `shm` 에서는 게이트웨이가 CAN 을 쥔 채로 상태를 발행해야 프록시가 붙는다.
    try:
        prepare_arms(follower_ifaces, purpose="inference")
    except ArmPrepareError as e:
        raise HTTPException(400, str(e))

    args = _build_args_for(body, robot_type, robot_port)

    slip_warns = _clear_arm_errors("inference-start", follower_ifaces)

    # ⚠ 지난 세션의 파라미터를 버린다. ZMQ 는 소켓을 닫으면 큐도 사라졌지만
    # Redis 리스트는 남아서, 안 비우면 이전 추론 끝에 민 슬라이더 값이
    # 새 추론 시작 직후에 적용된다 (refactor/daemon-split.md 3단계).
    await param_bridge.clear()

    env_extra = {"PIPER_DEBUG_DIR": settings.debug_dir} if body.debug_mode else None
    try:
        await process_manager.start(args, env_extra=env_extra)
    except Exception as e:
        raise HTTPException(500, f"프로세스 시작 실패: {e}")
    # 무엇이 도는지 남긴다 — 오케스트레이터가 `task` 를 보내도 되는 정책인지
    # 알아야 한다 (`services/inference_state` 참고).
    inference_state.set_running(body.policy_type, body.checkpoint_path)
    return {"status": "started", "pid": process_manager.pid, "args": args,
            "mode": body.inference_mode, "camera_profile": profile_report,
            "arm_reset": {"warnings": slip_warns}}


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
    # ⚠ 손으로 편집한 인자라 **정책을 확실히 알 수 없다.** 추측해서 적으면
    #   오케스트레이터가 그 추측 위에서 판단하게 된다 — 모른다고 남기는 편이
    #   낫다. `policy_type=""` 이면 아래 가드가 "확인 불가" 로 막는다.
    inference_state.set_running("", "")
    return {"status": "started", "pid": process_manager.pid, "args": body.args}


@router.post("/inference/stop")
async def stop_inference():
    await process_manager.stop()
    inference_state.clear()
    # 추론 정지 후(subprocess가 CAN을 놓은 뒤) 로봇팔 에러 플래그 조회 + 무조건 클리어
    _clear_arm_errors("inference-stop")
    return {"status": "stopped"}
