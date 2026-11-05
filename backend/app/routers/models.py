import json
import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from app.core.cli_mapping import (
    build_grpc_client_args, build_inference_args, checkpoint_dim_problem,
)
from app.core.config import settings
from app.core.inference_params import PARAM_SPEC, foreign_to
from app.services import action_space, target_input, train_cameras
from app.core.policies import takes_language
from app.services.exclusivity import Activity, require_idle
from app.services.camera_config import (
    CameraPrepareError,
    build_cameras_json,
    prepare_cameras,
    release_all_cameras,
)
from app.services.model_scanner import (
    delete_checkpoints, delete_model, extract_model_requirements, get_model, run_dir_of, scan_models, scan_runs,
)
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


# ⚠ `GET /{model_id:path}` 보다 **앞**에 있어야 한다 — 뒤에 두면 `inference/devices` 가 모델 id 로 읽힌다
@router.get("/inference/devices")
async def inference_devices():
    """로컬 추론이 쓸 수 있는 장치. 화면이 GPU 없는 기계에서 CUDA 를 못 고르게 한다.

    **로컬 모드에만 해당한다** — 서버(gRPC) 모드의 장치는 서버 기계의 것이라 여기서 알 수 없다.
    """
    from app.services import resources

    return {"cuda": resources.cuda_present()}


# ⚠ 이것도 `GET /{model_id:path}` 보다 **앞**에 있어야 한다 — 뒤에 있을 때는 "Model not found"(404) 가 나갔고,
#   SDK 가 그 404 를 "추론이 안 돈다" 로 읽어 도는 추론 앞에서 미션 플러그인이 멈췄다
@router.get("/inference/policy")
async def inference_policy():
    """지금 도는 정책 — 플러그인(MissionPlugin)이 "판단이 로봇에 닿는가"를 묻는 길 (feature/plugins.md §12-1).

    ACT 는 task 를 안 읽는다. 모르면 `takes_language: null` — 소비자는 낙관하지 않는다.

    정책을 여럿 올렸으면(feature/multi-policy.md) `policies`(이름 → 체크포인트)·`active`·`holding` 이 같이
    나가고 `checkpoint_path` 는 지금 정책의 것이다. `active` 는 wrapper 가 갈아끼운 뒤에 바뀐다.
    """
    cur = inference_state.get()
    if cur is None:
        return {"running": False, "type": None, "takes_language": None, "checkpoint_path": None,
                "policies": {}, "active": None, "holding": False}
    return {"running": True, "type": cur.policy_type or None,
            "takes_language": takes_language(cur.policy_type) if cur.policy_type else None,
            "checkpoint_path": cur.policies.get(cur.active, cur.checkpoint_path) or None,
            "policies": dict(cur.policies), "active": cur.active or None, "holding": cur.holding}


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


def _action_space_for(body) -> dict:
    """체크포인트의 액션 공간 (services/action_space.py). 돌리면 안 되는 조합이면 여기서 400.

    사람이 고르는 선택지가 없다 — 고를 수 있으면 틀릴 수 있다.
    """
    info = action_space.resolve(Path(body.checkpoint_path)) if body.checkpoint_path else \
        {"action_space": action_space.ABSOLUTE, "spec": None}
    if info["action_space"] == action_space.UNKNOWN:
        raise HTTPException(400, action_space.unknown_message(info))
    if info["action_space"] == action_space.DELTA_V1 and body.inference_mode == "server":
        # 델타를 절대로 되돌리는 것은 로컬 wrapper 다. 정책 서버는 액션만 돌려준다
        raise HTTPException(400, "델타 체크포인트는 로컬 추론에서만 돌 수 있습니다 — 정책 서버 모드는 절대 액션만 압니다")
    return info


def _target_for(body) -> dict:
    """체크포인트가 목표 위치를 입력으로 받나 (services/target_input.py). 돌릴 수 없는 조합이면 여기서 400.

    사람이 고르는 선택지가 없다 — 사양은 학습 데이터셋이 정한다.
    """
    info = target_input.resolve(Path(body.checkpoint_path)) if body.checkpoint_path else \
        {"input": target_input.NONE, "spec": None}
    if info["input"] == target_input.UNKNOWN:
        raise HTTPException(400, target_input.unknown_message(info))
    if info["input"] == target_input.TARGET_V1 and body.inference_mode == "server":
        # 목표 칸을 state 에 붙이는 것은 로컬 wrapper 다. 정책 서버 경로는 로봇 state 만 보낸다
        raise HTTPException(400, "목표 입력 체크포인트는 로컬 추론에서만 돌 수 있습니다 — 정책 서버 모드는 목표 칸을 만들지 못합니다")
    return info


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
    # 액션 공간 — 두 모드 모두 먼저 본다. 서버 모드에 델타를 걸면 미리보기에서부터 거절된다
    space = _action_space_for(body)
    target = _target_for(body)
    is_bimanual = len(body.robot_ports) >= 2

    # ⚠ **언어를 안 받는 정책에는 `task` 를 아예 안 싣는다.**
    # 화면은 ACT 일 때 입력란을 감추지만 값은 localStorage 에 남아 계속 실려 왔다.
    # 그래서 입력한 적 없는 옛 문장이 CLI 미리보기와 wrapper 로그에 떴다.
    # 감추는 것과 안 보내는 것은 다르고, **판정은 여기 한 곳**이다 —
    # 화면이 따로 판단하면 프리셋·CLI 직접 편집 같은 다른 경로가 새어 나간다.
    user_params = dict(body.params)
    if not takes_language(body.policy_type):
        user_params.pop("task", None)
    # 정책별 파라미터(`scoped`)도 같은 이유로 그 정책에만 싣는다. ACT 에서 켠 시간 앙상블이
    # localStorage 에 남아 SmolVLA 추론에 실리면, 스위치가 안 보이는 화면에서 매 스텝 추론이 돈다.
    foreign = foreign_to(body.policy_type)
    for key in foreign:
        user_params.pop(key, None)
    # 그 정책의 스위치는 요청에 없으면 스펙 기본값 — 플러그인·외부 API 처럼 화면 밖에서 띄워도 화면과
    # 같게 돈다. wrapper 기본값은 정책을 안 가려 꺼짐이다(ACT 의 시간 앙상블은 켬이 기본). 스위치만
    # 채운다 — 숫자는 wrapper·체크포인트에 제 기본값이 있다. 둘 다 로컬 wrapper 의 것이다.
    if body.inference_mode != "server":
        for key, spec in PARAM_SPEC.items():
            if spec.get("scoped") and spec.get("kind") == "bool" and key not in foreign:
                user_params.setdefault(key, spec["default"])

    if body.inference_mode == "server":
        # 정책을 여럿 올렸으면 여기서 400 — 정책 서버는 하나만 돌린다 (feature/multi-policy.md)
        _extra_policies_for(body)
        params = {
            "server_address": body.server_address,
            "robot_type": robot_type,
            "robot_port": body.robot_ports[0] if is_bimanual else robot_port,
            "checkpoint_path": body.checkpoint_path,
            "policy_type": body.policy_type,
            "policy_device": body.device,
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
        "device": body.device,
        # AMP(autocast)는 cuda 에서만 의미가 있다 — wrapper 도 cuda 가 아니면 쓰지 않지만
        # policy.config 에 켜진 채 남기지 않는다
        "use_amp": body.device == "cuda",
        "debug": body.debug_mode,
        **user_params,
    }
    if space["action_space"] == action_space.DELTA_V1:
        params["action_space"] = action_space.DELTA_V1
        params["delta_spec"] = space["spec"]
    # 목표 입력이면 사양을 싣는다. 아니면 아무것도 안 붙어 기존 명령이 한 글자도 안 바뀐다
    if target["input"] == target_input.TARGET_V1:
        params["target_spec"] = target["spec"]
    if cameras:
        params["cameras"] = cameras
    if is_bimanual:
        # 양팔은 이제 로컬도 된다 — wrapper 가 robot_ports 를 보고 bi 로봇을 조립한다.
        # robot_port 는 첫 팔로 남겨 둔다 (wrapper 의 단팔 폴백 인자)
        params["robot_port"] = body.robot_ports[0]
        params["robot_ports"] = body.robot_ports
    # 정책을 여럿 올리면(feature/multi-policy.md) 이름과 사양을 싣는다. 이름이 기본이고 하나뿐이면 아무것도
    # 안 붙어 기존 명령이 한 글자도 안 바뀐다
    extras = _extra_policies_for(body)
    name = getattr(body, "policy_name", DEFAULT_POLICY_NAME)
    if extras or name != DEFAULT_POLICY_NAME:
        params["policy_name"] = name
    if extras:
        params["extra_policies"] = _extra_policies_arg(extras)
    return build_inference_args(params)


# ── 여러 정책 (feature/multi-policy.md) ──

DEFAULT_POLICY_NAME = "main"


def _policy_name_ok(name: str) -> bool:
    """wrapper/policy_slots.NAME_RE 와 같은 규칙 — 이름은 로그·텔레메트리·REST 에 그대로 나간다."""
    return 0 < len(name) <= 40 and name.isascii() and all(c.isalnum() or c in "_-" for c in name)


class ExtraPolicy(BaseModel):
    name: str
    checkpoint_path: str


def _ckpt_config(path: str) -> dict:
    try:
        return json.loads((Path(path) / "config.json").read_text())
    except (OSError, ValueError):
        return {}


def _action_len(cfg: dict) -> int:
    shape = ((cfg.get("output_features") or {}).get("action") or {}).get("shape") or []
    return int(shape[0]) if len(shape) == 1 else 0


def _extra_policies_for(body) -> list[dict]:
    """더 올릴 정책마다 첫 정책과 같은 검사를 하고 wrapper 에 실을 사양을 만든다. 돌릴 수 없으면 400.

    장치를 건드리기 **전에** 부른다. 정책 종류·액션 길이가 첫 정책과 같아야 한다 — 한 로봇에서 바꿔 쓰고,
    정책별 파라미터 판정(`foreign_to`)·`task` 판정이 정책 종류 하나를 기준으로 돈다.
    액션 공간·목표 입력은 사람이 고르지 않는다 — 첫 정책처럼 체크포인트의 학습 데이터셋이 정한다."""
    name = getattr(body, "policy_name", DEFAULT_POLICY_NAME)
    if not _policy_name_ok(name):
        raise HTTPException(400, f"정책 이름이 틀렸습니다: {name} — 영숫자·_·- 40자까지")
    extras = getattr(body, "extra_policies", None) or []
    if not extras:
        return []
    if body.inference_mode == "server":
        raise HTTPException(400, "정책을 여럿 올리는 것은 로컬 추론에서만 됩니다 — 정책 서버는 정책 하나만 돌립니다")
    first = _ckpt_config(body.checkpoint_path)
    first_type = first.get("type") or body.policy_type
    first_len = _action_len(first)
    seen, out = {name}, []
    for x in extras:
        if not _policy_name_ok(x.name):
            raise HTTPException(400, f"정책 이름이 틀렸습니다: {x.name} — 영숫자·_·- 40자까지")
        if x.name in seen:
            raise HTTPException(400, f"정책 이름이 겹칩니다: {x.name}")
        seen.add(x.name)
        cfg = _ckpt_config(x.checkpoint_path)
        if not cfg:
            raise HTTPException(400, f"정책 {x.name} 의 체크포인트를 읽을 수 없습니다: {x.checkpoint_path}")
        kind = cfg.get("type")
        if kind != first_type:
            raise HTTPException(400, f"정책 {x.name} 의 종류({kind})가 첫 정책({first_type})과 다릅니다 — "
                                     f"한 추론에는 같은 종류만 올립니다")
        length = _action_len(cfg)
        if first_len and length and length != first_len:
            raise HTTPException(400, f"정책 {x.name} 의 액션 길이({length})가 첫 정책({first_len})과 다릅니다 — "
                                     f"같은 로봇 구성으로 학습한 정책만 같이 올립니다")
        # 카메라는 매핑된 것만 연결된다 — 학습 카메라 대조는 매핑에 없는 카메라를 "모름" 으로 넘기므로 여기서 본다.
        # 빠지면 시작은 되고 그 정책으로 바꾸는 순간부터 매 추론이 관측 키 없음으로 죽는다.
        # 매핑이 아예 없는 요청(CLI 초기화의 미리보기 등)은 첫 정책도 대조하지 않는다 — 같은 규칙
        mapped = getattr(body, "camera_mapping", None) or {}
        cams = [c["name"] for c in extract_model_requirements(cfg, Path(x.checkpoint_path))["required_cameras"]]
        missing = [c for c in cams if c not in mapped] if mapped else []
        if missing:
            raise HTTPException(400, f"정책 {x.name} 이 쓰는 카메라({', '.join(missing)})가 매핑에 없습니다 — "
                                     f"첫 정책과 같은 카메라로 학습한 정책만 같이 올립니다")
        problem = checkpoint_dim_problem(x.checkpoint_path)
        if problem:
            raise HTTPException(400, problem)
        space = action_space.resolve(Path(x.checkpoint_path))
        if space["action_space"] == action_space.UNKNOWN:
            raise HTTPException(400, action_space.unknown_message(space))
        target = target_input.resolve(Path(x.checkpoint_path))
        if target["input"] == target_input.UNKNOWN:
            raise HTTPException(400, target_input.unknown_message(target))
        out.append({"name": x.name, "path": x.checkpoint_path, "space": space, "target": target})
    return out


def _extra_policies_arg(extras: list[dict]) -> str:
    """wrapper `--extra-policies` 값 (wrapper/policy_slots.parse_extra 가 읽는다)."""
    return json.dumps([{
        "name": x["name"], "path": x["path"],
        "action_space": x["space"]["action_space"],
        "delta_spec": x["space"]["spec"] if x["space"]["action_space"] == action_space.DELTA_V1 else {},
        "target_spec": x["target"]["spec"] if x["target"]["input"] == target_input.TARGET_V1 else None,
    } for x in extras], ensure_ascii=False, separators=(",", ":"))


@router.get("")
async def list_models():
    return scan_models()


# ⚠ 아래 `/runs…` 는 `/{model_id:path}` 캐치올보다 **위**에 있어야 한다 — 뒤에 두면 `runs` 가 모델 id 로 읽힌다
@router.get("/runs")
async def list_runs():
    """가중치 표 — 학습 하나가 한 행 (model_scanner.scan_runs)."""
    return scan_runs()


class ModelNotesRequest(BaseModel):
    name: str = ""
    description: str = ""


@router.get("/runs/{run:path}/notes")
async def get_run_notes(run: str):
    from app.services.notes_sidecar import read_notes

    run_dir = run_dir_of(run)
    if run_dir is None:
        raise HTTPException(404, "학습을 찾을 수 없습니다")
    return read_notes(run_dir, kind="model")


@router.put("/runs/{run:path}/notes")
async def put_run_notes(run: str, body: ModelNotesRequest):
    """학습 단위 이름·설명 — 학습 폼의 제목·설명과 **같은 파일**(run 루트 piper_notes.json).
    체크포인트는 자기 사이드카가 없으면 이것을 물려받는다."""
    from app.services.notes_sidecar import write_notes

    run_dir = run_dir_of(run)
    if run_dir is None:
        raise HTTPException(404, "학습을 찾을 수 없습니다")
    return write_notes(run_dir, kind="model", name=body.name, description=body.description)


class PruneRequest(BaseModel):
    #: 지울 체크포인트 id — 전부 이 학습의 것이어야 한다
    ids: list[str]


@router.post("/runs/{run:path}/prune")
async def prune_run(run: str, body: PruneRequest):
    """한 학습에서 사람이 고른 체크포인트만 지운다 (model_scanner.delete_checkpoints)."""
    if not body.ids:
        raise HTTPException(400, "지울 체크포인트를 고르세요")
    try:
        return delete_checkpoints(run, body.ids)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


# 이름·설명 사이드카 — 상세 catch-all 이 path 를 통째로 삼키므로 **위에** 둔다.
@router.get("/{model_id:path}/notes")
async def get_model_notes(model_id: str):
    from pathlib import Path

    from app.services.notes_sidecar import read_notes

    model = get_model(model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    return read_notes(Path(model["path"]), kind="model")


@router.put("/{model_id:path}/notes")
async def put_model_notes(model_id: str, body: ModelNotesRequest):
    from pathlib import Path

    from app.services.notes_sidecar import write_notes

    model = get_model(model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    return write_notes(Path(model["path"]), kind="model",
                       name=body.name, description=body.description)


class ModelUploadRequest(BaseModel):
    #: 올릴 Hub 모델 저장소 이름. **이름만** 주면 접속된 계정 아래로 간다(화면은 이 모양만 보낸다).
    #: `네임스페이스/이름` 을 주면 그대로 쓴다 — 외부 호출이 조직을 직접 고를 길이다. 모델 id 는 학습
    #: 경로(`2026-10-27/…/020000`)라 그대로 저장소 이름이 될 수 없어서 사람이 정한다.
    repo_id: str
    private: bool = False


@router.post("/{model_id:path}/upload")
async def upload_model_to_hub(model_id: str, body: ModelUploadRequest):
    """체크포인트 하나(`pretrained_model` 폴더)를 Hub 모델 저장소로 올린다.

    지금까지 모델이 Hub 로 가는 길은 학습 폼의 Policy Repo ID(학습이 끝날 때 LeRobot 이 올린다)
    하나뿐이라, 이미 끝난 학습의 체크포인트는 올릴 수 없었다. 올라가는 것은 그 길과 같은 묶음 —
    config·가중치·전처리 파일·train_config 다.

    데이터셋 업로드와 **같은 작업 유닛**(`piper-xfer-upload`)·같은 로그 창을 쓴다. 둘 다 느린
    네트워크 작업이라 동시에 하나만 돈다. 데이터셋과 달리 코드베이스 태그는 없다 — 모델은
    `--policy.path` 로 받을 때 태그를 묻지 않는다.
    """
    import re

    from app.routers.datasets import _find_hf_cli, _upload_pm
    from app.services import hub_client
    from app.services.notes_sidecar import ensure_readme

    repo_id = body.repo_id.strip()
    if not re.fullmatch(r"([\w.-]+/)?[\w.-]+", repo_id):
        raise HTTPException(400, "저장소 이름에는 영문·숫자·밑줄·점·하이픈만 쓸 수 있습니다")
    model = get_model(model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    require_idle(Activity.UPLOAD)
    hf_cli = _find_hf_cli()
    if not hf_cli:
        raise HTTPException(500, "HuggingFace CLI(hf)를 찾을 수 없습니다. 설정에서 경로를 지정하세요.")
    account = await hub_client.require_hub_write()
    if "/" not in repo_id:
        repo_id = hub_client.repo_under(account, repo_id)

    model_dir = Path(model["path"])
    # 이름·설명이 있으면 허브 카드(README.md)로 — 이미 있으면 안 건드린다. ⚠ 학습 산출물에만 쓴다:
    #   허브에서 받은 모델의 폴더는 HF 캐시 스냅샷이라 거기 쓰면 공유 캐시를 고친다
    if model_dir.name == "pretrained_model":
        ensure_readme(model_dir, repo_id, model.get("notes") or {})
    args = [hf_cli, "upload", repo_id, str(model_dir), ".", "--repo-type=model",
            f"--commit-message=Upload {model_id}"]
    if body.private:
        args.append("--private")
    await _upload_pm.start(args)
    return {"status": "started", "model_id": model_id, "repo_id": repo_id, "pid": _upload_pm.pid}


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
    # 정책을 올릴 장치. 로컬은 wrapper `--device`, 서버는 `--policy-device` 로 간다.
    # 기본은 예전 그대로 cuda — GPU 가 없는 기계에서는 사람이 cpu 를 고른다.
    device: Literal["cuda", "cpu"] = "cuda"
    server_address: str = "127.0.0.1:8088"
    policy_type: str = "act"
    actions_per_chunk: int = 100
    aggregate_fn: str = "weighted_average"
    offset_correction: bool = False
    smoothing: str = "none"
    smoothing_window: int = 5
    debug_mode: bool = False
    # 정책 여럿 (feature/multi-policy.md) — 같은 프로세스에 올려 두고 `POST /api/params/policy` 로 바꿔 쓴다.
    # 첫 정책은 `checkpoint_path` 이고 이름이 `policy_name`. 로컬 모드만
    policy_name: str = DEFAULT_POLICY_NAME
    extra_policies: list[ExtraPolicy] = []


class InferencePreviewRequest(BaseModel):
    checkpoint_path: str
    robot_type: str | None = None
    robot_port: str | None = None
    robot_ports: list[str] = []
    camera_mapping: dict[str, str] = {}
    inference_mode: str = "local"
    device: Literal["cuda", "cpu"] = "cuda"
    server_address: str = "127.0.0.1:8088"
    policy_type: str = "act"
    aggregate_fn: str = "weighted_average"
    offset_correction: bool = False
    smoothing: str = "none"
    smoothing_window: int = 5
    actions_per_chunk: int = 100
    params: dict = {}
    debug_mode: bool = False
    # 미리보기가 실제 실행과 같은 명령을 보이게 — 시작 요청과 같은 필드
    policy_name: str = DEFAULT_POLICY_NAME
    extra_policies: list[ExtraPolicy] = []


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


def _checkpoint_flag_value(args: list[str]) -> str:
    """직접 편집한 추론 인자에서 체크포인트 경로를 찾는다 (`--flag v` · `--flag=v` 둘 다)."""
    flags = ("--policy-path", "--pretrained-path")
    for i, a in enumerate(args):
        for flag in flags:
            if a == flag and i + 1 < len(args):
                return args[i + 1]
            if a.startswith(flag + "="):
                return a.split("=", 1)[1]
    return ""


@router.post("/inference/start")
async def start_inference(body: InferenceStartRequest):
    require_idle(Activity.INFERENCE)
    # ⚠ **화면의 검증(`/inference/validate`)만 믿지 않는다.** 그건 화면이 부르는 것이라 API 로
    #   직접 들어오면 안 거친다. 어긋난 체크포인트는 시작한 뒤 매 프레임 정규화에서 죽는다.
    problem = checkpoint_dim_problem(body.checkpoint_path)
    if problem:
        raise HTTPException(400, problem)
    # 카메라·팔을 준비하기 전에 막는다 — 돌릴 수 없는 체크포인트면 장치를 건드릴 이유가 없다
    space = _action_space_for(body)
    target = _target_for(body)
    robot_type = body.robot_type or robot_manager.selected_type
    if not robot_type:
        raise HTTPException(400, "로봇이 선택되지 않았습니다. 로봇 페이지에서 먼저 선택하세요.")
    is_bimanual = len(body.robot_ports) >= 2
    robot_port = body.robot_port or _get_first_ready_follower_port()
    if not is_bimanual and not robot_port:
        raise HTTPException(400, "등록된 follower가 없습니다. 로봇 페이지에서 먼저 등록하세요.")
    # 더 올릴 정책도 장치를 건드리기 전에 본다 (feature/multi-policy.md)
    extras = _extra_policies_for(body)

    # ⚠ **카메라를 먼저 준비한다.** `shm` 에서는 세그먼트에서 실제 해상도를 읽어
    # 인자에 싣는데, 연결 전이면 세그먼트가 없어 기본값(640x480)으로 떨어진다 —
    # D405 처럼 848x480 인 카메라가 어긋난 채로 데이터셋 메타에 박힌다.
    try:
        prepare_cameras(body.camera_mapping, purpose="inference")
    except CameraPrepareError as e:
        raise HTTPException(400, str(e))

    # 학습 때 카메라와 대조 — 연결된 **뒤에** 본다(세그먼트 크기가 있어야 한다). 비율이 다르면 막고,
    # 크기만 다르면 경고를 실어 시작한다 (feature/camera-resolution.md). 화면의 검증과 같은 판정이다.
    trained = (train_cameras.resolve(Path(body.checkpoint_path)) if body.checkpoint_path
               else {"cameras": {}, "source": "none"})
    cam_errors, cam_warnings = train_cameras.messages(
        train_cameras.compare(trained["cameras"], train_cameras.live_cameras(body.camera_mapping)))
    if cam_errors:
        raise HTTPException(400, cam_errors[0])
    # 더 올릴 정책도 같은 판정 — 바꿔 쓰는 정책마다 지금 카메라를 본다
    for x in extras:
        x["trained"] = train_cameras.resolve(Path(x["path"]))
        x_errors, x_warnings = train_cameras.messages(
            train_cameras.compare(x["trained"]["cameras"], train_cameras.live_cameras(body.camera_mapping)))
        if x_errors:
            raise HTTPException(400, x_errors[0])
        cam_warnings += [w for w in x_warnings if w not in cam_warnings]

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
    # 표식(액션 공간·목표 입력·학습 카메라)은 더 올릴 정책에도 남긴다 — 아래 첫 정책과 같은 이유
    for x in extras:
        action_space.remember(Path(x["path"]), x["space"])
        target_input.remember(Path(x["path"]), x["target"])
        train_cameras.remember(Path(x["path"]), x["trained"])

    slip_warns = _clear_arm_errors("inference-start", follower_ifaces)

    # ⚠ 지난 세션의 파라미터를 버린다. ZMQ 는 소켓을 닫으면 큐도 사라졌지만
    # Redis 리스트는 남아서, 안 비우면 이전 추론 끝에 민 슬라이더 값이
    # 새 추론 시작 직후에 적용된다 (refactor/daemon-split.md 3단계).
    await param_bridge.clear()

    # 델타로 판정했으면 체크포인트에 표식을 남긴다 — 학습 데이터셋이 나중에 지워져도 델타로 남고,
    # wrapper 가 손으로 고친 인자와 대조한다. **시작에서만** 한다 (미리보기는 파일을 안 고친다).
    if body.checkpoint_path:
        action_space.remember(Path(body.checkpoint_path), space)
        # 목표 입력 사양도 같은 이유로 — 데이터셋을 지운 뒤에도 같은 칸을 만들고, 손으로 고친 인자와 대조한다
        target_input.remember(Path(body.checkpoint_path), target)
        # 학습 카메라도 같은 이유로 — 데이터셋을 지운 뒤에도 해상도 대조가 된다
        train_cameras.remember(Path(body.checkpoint_path), trained)

    env_extra = {"PIPER_DEBUG_DIR": settings.debug_dir} if body.debug_mode else None
    try:
        await process_manager.start(args, env_extra=env_extra)
    except Exception as e:
        raise HTTPException(500, f"프로세스 시작 실패: {e}")
    # 무엇이 도는지 남긴다 — 오케스트레이터가 `task` 를 보내도 되는 정책인지
    # 알아야 한다 (`services/inference_state` 참고).
    inference_state.set_running(
        body.policy_type, body.checkpoint_path,
        policies={body.policy_name: body.checkpoint_path, **{x["name"]: x["path"] for x in extras}},
        active=body.policy_name)
    return {"status": "started", "pid": process_manager.pid, "args": args,
            "mode": body.inference_mode, "camera_profile": profile_report,
            "arm_reset": {"warnings": slip_warns},
            "cameras": {"warnings": cam_warnings}}


@router.post("/inference/start-custom")
async def start_inference_custom(body: InferenceStartCustomRequest):
    """직접 편집한 CLI 인자로 추론 시작."""
    # 카메라를 해제하기 전에 막는다 — 녹화 중이면 여기서 뺏으면 안 된다
    require_idle(Activity.INFERENCE)
    if not body.args:
        raise HTTPException(400, "CLI 인자가 비어있습니다")
    # ⚠ 직접 편집한 인자도 같은 체크포인트를 돈다 — 위 `/inference/start` 와 같은 이유다.
    problem = checkpoint_dim_problem(_checkpoint_flag_value(body.args))
    if problem:
        raise HTTPException(400, problem)
    # 손으로 고친 인자에서 델타 플래그를 빼면 델타 체크포인트가 절대로 돈다 — 팔이 영점 자세로 돌진한다
    ckpt = _checkpoint_flag_value(body.args)
    if ckpt:
        info = action_space.resolve(Path(ckpt))
        if info["action_space"] == action_space.UNKNOWN:
            raise HTTPException(400, action_space.unknown_message(info))
        flagged = (body.args[body.args.index("--action-space") + 1]
                   if "--action-space" in body.args[:-1] else action_space.ABSOLUTE)
        if flagged != info["action_space"]:
            raise HTTPException(400, f"액션 공간이 어긋납니다 — 체크포인트는 {info['action_space']}, 인자는 {flagged}. "
                                     f"편집 전 명령의 --action-space·--delta-spec 을 그대로 두세요")
        # 목표 입력도 — 사양을 빼면 wrapper 가 길이로 막지만, 다른 사양으로 바꾸면 목표가 엉뚱한 칸에 들어간다
        tinfo = target_input.resolve(Path(ckpt))
        if tinfo["input"] == target_input.UNKNOWN:
            raise HTTPException(400, target_input.unknown_message(tinfo))
        has_flag = "--target-spec" in body.args[:-1]
        wanted = tinfo["input"] == target_input.TARGET_V1
        if wanted and not has_flag:
            raise HTTPException(400, "이 체크포인트는 목표 위치를 입력으로 받는데 인자에 --target-spec 이 없습니다. "
                                     "편집 전 명령의 --target-spec 을 그대로 두세요")
        if has_flag and not wanted:
            raise HTTPException(400, "이 체크포인트는 목표 위치를 입력으로 받지 않는데 인자에 --target-spec 이 있습니다. "
                                     "--target-spec 을 지우세요")
        if has_flag:
            try:
                given = json.loads(body.args[body.args.index("--target-spec") + 1])
                same = (given.get("names") == tinfo["spec"].get("names")
                        and given.get("freqs") == tinfo["spec"].get("freqs"))
            except (ValueError, AttributeError):
                same = False
            if not same:
                raise HTTPException(400, "--target-spec 이 이 체크포인트의 학습 사양과 다릅니다. "
                                         "편집 전 명령의 --target-spec 을 그대로 두세요")
        # 화면 비율도 같은 규칙 — 손으로 고친 `--cameras` 의 세그먼트를 직접 읽어 대조한다
        if "--cameras" in body.args[:-1]:
            live = train_cameras.live_from_cameras_arg(body.args[body.args.index("--cameras") + 1])
            cam_errors, _ = train_cameras.messages(
                train_cameras.compare(train_cameras.resolve(Path(ckpt))["cameras"], live))
            if cam_errors:
                raise HTTPException(400, cam_errors[0])

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
