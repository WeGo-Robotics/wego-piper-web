"""
LeRobot CLI 인자 매핑 테이블.
LeRobot 업데이트 시 이 파일만 수정하면 됨.
"""

import logging
from pathlib import Path

from app.core import inference_params
from app.core.config import settings

# wrapper 경로 (프로젝트 내 상대경로)
WRAPPER_PATH = str(Path(__file__).resolve().parents[3] / "wrapper" / "lerobot_wrapper.py")

# 데이터 수집 CLI 명령어
RECORD_CMD = "lerobot-record"

# 데이터셋 편집 CLI 명령어
EDIT_DATASET_CMD = "lerobot-edit-dataset"

# `direct` 타입 → `shm` 프록시 타입 (refactor/robot-transport.md).
# 프록시는 `PiperFollower` 를 상속해 `__init__` 만 바꾸므로 관측·행동 계약이 같다.
# 여기 없는 타입(leader 등)은 **그대로 둔다** — 조용히 바꾸면 있지도 않은
# `robot.type` 으로 subprocess 가 죽는다.
SHM_ROBOT_TYPES: dict[str, str] = {"piper_follower": "piper_follower_shm"}


def resolve_robot_type(robot_type: str) -> str:
    """전송 방식에 맞는 `robot.type`.

    ⚠ **인자를 만드는 쪽에서 부른다.** 라우터마다 분기하면 미리보기와 실행이
    갈리고, 더 나쁘게는 추론과 녹화가 서로 다른 드라이버로 도는 순간이 생긴다 —
    그러면 같은 팔에 두 프로세스가 CAN 을 연다.
    """
    if settings.robot_transport != "shm":
        return robot_type
    return SHM_ROBOT_TYPES.get(robot_type, robot_type)

# wrapper 인자 매핑 (lerobot_wrapper.py 기준)
INFERENCE_ARGS_MAP: dict[str, str] = {
    "checkpoint_path": "--policy-path",
    "robot_type": "--robot-type",
    "robot_port": "--robot-port",
    "fps": "--fps",
    "device": "--device",
    "use_amp": "--use-amp",
    "task": "--task",
    "cameras": "--cameras",
    "debug": "--debug",
}

# 데이터셋 편집 operation 매핑
EDIT_OPERATIONS: dict[str, str] = {
    "delete_episodes": "delete_episodes",
    "split": "split",
    "merge": "merge",
    "remove_features": "remove_features",
    "info": "info",
}


# CLI 플래그가 아니라 `--config-overrides` JSON으로 실어 보낼 키.
# **PARAM_SPEC 에서 파생한다** (`send_at_start=True`).
#
# 이 값들은 실행 중 버스로도 바꿀 수 있지만, **시작값도 반드시 이 경로로 전달해야 한다** —
# 여기 없으면 wrapper 기본값으로 떨어져 UI에서 맞춘 값이 조용히 유실된다(에러 없음).
# 로컬 wrapper 와 gRPC wrapper 가 같은 집합을 쓴다.
OVERRIDE_KEYS: set[str] = inference_params.start_params()


def _split_overrides(params: dict) -> tuple[dict, dict]:
    """(CLI 인자로 갈 것, --config-overrides 로 갈 것) 으로 나눈다."""
    cli_params, overrides = {}, {}
    for key, value in params.items():
        (overrides if key in OVERRIDE_KEYS else cli_params)[key] = value
    return cli_params, overrides


def build_inference_args(params: dict) -> list[str]:
    """웹 UI 파라미터를 wrapper CLI 인자 리스트로 변환."""
    args = [settings.local_python, "-u", WRAPPER_PATH]  # -u: unbuffered stdout

    overrides = {}

    for key, value in params.items():
        if key in OVERRIDE_KEYS:
            overrides[key] = value
            continue
        cli_flag = INFERENCE_ARGS_MAP.get(key)
        if cli_flag is None:
            continue
        if isinstance(value, bool):
            if value:
                args.append(cli_flag)
        elif isinstance(value, dict):
            import json
            args.extend([cli_flag, json.dumps(value, separators=(",", ":"))])
        else:
            args.extend([cli_flag, str(value)])

    if overrides:
        import json
        args.extend(["--config-overrides", json.dumps(overrides, separators=(",", ":"))])

    return args


# ── 모드 B: gRPC 서버-클라이언트 ──

# gRPC 모드
GRPC_WRAPPER_PATH = str(Path(__file__).resolve().parents[3] / "wrapper" / "grpc_wrapper.py")

GRPC_CLIENT_ARGS_MAP: dict[str, str] = {
    "server_address": "--server-address",
    "robot_type": "--robot-type",
    "robot_port": "--robot-port",
    "robot_ports": "--robot-ports",
    "cameras": "--cameras",
    "checkpoint_path": "--pretrained-path",
    "policy_type": "--policy-type",
    "policy_device": "--policy-device",
    "actions_per_chunk": "--actions-per-chunk",
    "chunk_size_threshold": "--chunk-size-threshold",
    "aggregate_fn": "--aggregate-fn",
    "offset_correction": "--offset-correction",
    "smoothing": "--smoothing",
    "smoothing_window": "--smoothing-window",
    "task": "--task",
    "fps": "--fps",
    "debug": "--debug",
}


def build_grpc_client_args(params: dict) -> list[str]:
    """gRPC grpc_wrapper.py CLI 인자 빌더.

    로컬 모드와 동일하게 필터/정책 파라미터는 `--config-overrides` 로 묶어 보낸다.
    이전에는 이 경로가 없어서 UI 슬라이더 값이 시작 시 전부 유실됐다
    (wrapper 전역 기본값이 UI 기본값과 우연히 같아 티가 안 났다).
    """
    args = [settings.grpc_python, "-u", GRPC_WRAPPER_PATH]
    cli_params, overrides = _split_overrides(params)

    for key, value in cli_params.items():
        cli_flag = GRPC_CLIENT_ARGS_MAP.get(key)
        if cli_flag is None:
            continue
        if isinstance(value, bool):
            if value:
                args.append(cli_flag)
        elif isinstance(value, list):
            args.extend([cli_flag, ",".join(str(v) for v in value)])
        elif isinstance(value, dict):
            import json
            args.extend([cli_flag, json.dumps(value, separators=(",", ":"))])
        else:
            args.extend([cli_flag, str(value)])

    if overrides:
        import json
        args.extend(["--config-overrides", json.dumps(overrides, separators=(",", ":"))])

    return args


def build_edit_dataset_args(
    repo_id: str,
    operation: str,
    operation_params: dict | None = None,
) -> list[str]:
    """데이터셋 편집 CLI 인자 리스트 생성."""
    op_type = EDIT_OPERATIONS.get(operation)
    if op_type is None:
        raise ValueError(f"Unknown operation: {operation}")

    args = [
        EDIT_DATASET_CMD,
        f"--repo-id={repo_id}",
        f"--operation.type={op_type}",
    ]
    if operation_params:
        for key, value in operation_params.items():
            args.append(f"--operation.{key}={value}")
    return args


# ── 학습 ──

TRAIN_CMD = "lerobot-train"

TRAIN_ARGS_MAP: dict[str, str] = {
    "dataset_repo_id": "--dataset.repo_id",
    "policy_type": "--policy.type",
    "pretrained_path": "--policy.path",
    "policy_repo_id": "--policy.repo_id",
    "output_dir": "--output_dir",
    "batch_size": "--batch_size",
    "steps": "--steps",
    "log_freq": "--log_freq",
    "save_freq": "--save_freq",
    "eval_freq": "--eval_freq",
    "num_workers": "--num_workers",
    "seed": "--seed",
    "device": "--policy.device",
    "optimizer_type": "--optimizer.type",
    "learning_rate": "--optimizer.lr",
    "wandb_enable": "--wandb.enable",
    "wandb_project": "--wandb.project",
    "use_policy_training_preset": "--use_policy_training_preset",
}


def resolve_rename_map(pretrained_path: str) -> str:
    """pretrained 모델의 preprocessor 에서 rename_map(카메라 이름 매핑) 추출.

    **로컬 파일시스템을 읽는다.** 원격 학습에서는 호출부가 다른 방법으로 구해야 하므로
    인자 조립(`build_train_args`)과 분리해 둔다 (feature/cloud-training.md §1-(4)).
    """
    import json

    pre_path = Path(pretrained_path) / "policy_preprocessor.json"
    if not pre_path.exists():
        return ""
    try:
        pre_data = json.loads(pre_path.read_text())
        for step in pre_data.get("steps", []):
            if step.get("registry_name") == "rename_observations_processor":
                rm = step.get("config", {}).get("rename_map", {})
                if rm:
                    return json.dumps(rm)
    except Exception:
        pass
    return ""


def apply_dim_overrides(pretrained_path: str, state_dim: int, action_dim: int) -> bool:
    """체크포인트의 `config.json` 을 **수정한다**. 바꿨으면 True.

    ⚠ 파괴적이다. 이전에는 `build_train_args()` 안에 있어서
    **`/training/preview`(미리보기)도 체크포인트를 수정했다.**
    이제 학습 시작 경로에서만 호출한다.

    ⚠ 원격 학습에서는 체크포인트가 원격에 있어 불가능하다 —
    러너가 지원하지 않으면 **명시적으로 에러를 내야 한다** (조용한 무시 금지).
    """
    import json

    if not (state_dim or action_dim):
        return False
    config_path = Path(pretrained_path) / "config.json"
    if not config_path.exists():
        return False
    try:
        cfg = json.loads(config_path.read_text())
        modified = False
        if state_dim and state_dim > 0 and "observation.state" in cfg.get("input_features", {}):
            cfg["input_features"]["observation.state"]["shape"] = [state_dim]
            modified = True
        if action_dim and action_dim > 0 and "action" in cfg.get("output_features", {}):
            cfg["output_features"]["action"]["shape"] = [action_dim]
            modified = True
        if modified:
            config_path.write_text(json.dumps(cfg, indent=2))
            logging.getLogger(__name__).info(
                "Updated config.json: state=%s action=%s", state_dim, action_dim
            )
        return modified
    except Exception:
        return False


# 비전 백본을 ImageNet 가중치로 시작해야 하는 정책.
# LeRobot 기본값이 `None`(랜덤)이라 명시하지 않으면 조용히 맨바닥에서 시작한다.
# ACT 는 자체 기본값이 이미 ImageNet 이라 빠져 있다.
_IMAGENET_BACKBONE_POLICIES = {"diffusion", "vqbet"}


def build_train_args(
    params: dict,
    *,
    python: str | None = None,
    rename_map: str = "",
) -> list[str]:
    """학습 CLI 인자 빌더 — **순수 함수. 파일시스템을 만지지 않는다.**

    `python` 을 주면 인터프리터를 바꿔 끼울 수 있다 (원격은 로컬 conda 경로가 아니다).
    `rename_map` 은 호출부가 `resolve_rename_map()` 으로 미리 구해서 넘긴다.
    """
    args = [python or settings.grpc_python, "-m", "lerobot.scripts.lerobot_train"]

    # --policy.path와 --policy.type은 동시에 사용 불가
    has_pretrained = bool(params.get("pretrained_path"))

    for key, value in params.items():
        if has_pretrained and key == "policy_type":
            continue  # pretrained_path가 있으면 policy_type 스킵
        cli_flag = TRAIN_ARGS_MAP.get(key)
        if cli_flag is None:
            continue
        if isinstance(value, bool):
            args.append(f"{cli_flag}={'true' if value else 'false'}")
        else:
            args.append(f"{cli_flag}={value}")

    # policy.push_to_hub는 기본 True → repo_id 없으면 validate()에서 에러.
    # 로컬 학습(policy_repo_id 미지정)이면 Hub 푸시를 명시적으로 비활성화.
    # ⚠ 클라우드 학습에서는 이게 **체크포인트 회수 경로를 지우는 설정**이다
    #   (feature/cloud-training.md §6) → 러너별로 갈라야 한다.
    if not params.get("policy_repo_id"):
        args.append("--policy.push_to_hub=false")

    # use_policy_training_preset=false면 scheduler 필수 → 기본값 자동 추가
    if not params.get("use_policy_training_preset", True):
        if not any(a.startswith("--scheduler.type") for a in args):
            args.append("--scheduler.type=diffuser")

    # resume: --policy.path와 함께 사용 불가 (checkpoint_path가 None이 되어 에러)
    if params.get("resume") and not has_pretrained:
        args.append("--resume=true")

    # 정책별 config 오버라이드: --policy.<field>=<value>
    # 단, pretrained_path가 있으면 아키텍처는 체크포인트 config로 고정 → skip
    policy_params = params.get("policy_params") or {}
    if policy_params and not has_pretrained:
        for k, v in policy_params.items():
            val = "true" if v is True else "false" if v is False else v
            args.append(f"--policy.{k}={val}")

    # SmolVLA를 처음부터 학습할 때 load_vlm_weights를 빠뜨리면 LeRobot 기본값(false)이 적용되어
    # VLM 전체(SigLIP 비전 인코더 포함)가 랜덤 초기화된다. freeze_vision_encoder 기본값이 true라
    # 그 랜덤 가중치는 학습조차 되지 않는다. UI가 값을 보내지 않는 경우(오래된 localStorage 등)를
    # 위한 안전망. 파인튜닝(has_pretrained)은 체크포인트에서 가중치가 오므로 false가 정상이다.
    if (
        params.get("policy_type") == "smolvla"
        and not has_pretrained
        and "load_vlm_weights" not in policy_params
    ):
        args.append("--policy.load_vlm_weights=true")

    # Diffusion/VQ-BeT 는 `pretrained_backbone_weights` 기본값이 **None** 이라
    # ResNet18 이 랜덤 초기화된다 (`get_model(..., weights=None)`).
    # smolvla 의 `load_vlm_weights=False` 와 정확히 같은 함정이다.
    #
    # ACT 만 기본값이 `ResNet18_Weights.IMAGENET1K_V1` 이라 그대로 둬도 된다 —
    # 그래서 ACT 는 베이스 체크포인트를 받을 필요가 없다.
    if (
        params.get("policy_type") in _IMAGENET_BACKBONE_POLICIES
        and not has_pretrained
        and "pretrained_backbone_weights" not in policy_params
    ):
        args.append("--policy.pretrained_backbone_weights=ResNet18_Weights.IMAGENET1K_V1")

    rename_map = params.get("rename_map") or rename_map
    if rename_map:
        args.append(f"--rename_map={rename_map}")

    return args



# ── 레코딩 ──

RECORD_ARGS_MAP: dict[str, str] = {
    "robot_type": "--robot.type",
    "robot_port": "--robot.port",
    "robot_id": "--robot.id",
    "robot_cameras": "--robot.cameras",
    "teleop_type": "--teleop.type",
    "teleop_port": "--teleop.port",
    "teleop_id": "--teleop.id",
    "repo_id": "--dataset.repo_id",
    "single_task": "--dataset.single_task",
    "num_episodes": "--dataset.num_episodes",
    "fps": "--dataset.fps",
    "episode_time_s": "--dataset.episode_time_s",
    "reset_time_s": "--dataset.reset_time_s",
    "streaming_encoding": "--dataset.streaming_encoding",
    "vcodec": "--dataset.vcodec",
    "encoder_threads": "--dataset.encoder_threads",
    "encoder_queue_maxsize": "--dataset.encoder_queue_maxsize",
    "push_to_hub": "--dataset.push_to_hub",
    "private": "--dataset.private",
    "display_data": "--display_data",
}


RECORD_WRAPPER_PATH = str(Path(__file__).resolve().parents[3] / "wrapper" / "start_record.py")


def build_record_args(params: dict) -> list[str]:
    """레코딩 CLI 인자 빌더."""
    args = [settings.grpc_python, "-u", RECORD_WRAPPER_PATH]
    # 미리보기와 실행이 같은 조립기를 쓰므로 여기서 한 번만 바꾸면 둘 다 맞는다
    params = {**params, "robot_type": resolve_robot_type(params.get("robot_type", ""))}

    for key, value in params.items():
        cli_flag = RECORD_ARGS_MAP.get(key)
        if cli_flag is None:
            continue
        if isinstance(value, bool):
            args.append(f"{cli_flag}={'true' if value else 'false'}")
        elif isinstance(value, dict):
            import json
            args.append(f"{cli_flag}={json.dumps(value, separators=(',', ':'))}")
        else:
            args.append(f"{cli_flag}={value}")

    if params.get("resume"):
        args.append("--resume=true")

    return args
