"""
LeRobot CLI 인자 매핑 테이블.
LeRobot 업데이트 시 이 파일만 수정하면 됨.
"""

from pathlib import Path

from app.core.config import settings

# wrapper 경로 (프로젝트 내 상대경로)
WRAPPER_PATH = str(Path(__file__).resolve().parents[3] / "wrapper" / "lerobot_wrapper.py")

# 데이터 수집 CLI 명령어
RECORD_CMD = "lerobot-record"

# 데이터셋 편집 CLI 명령어
EDIT_DATASET_CMD = "lerobot-edit-dataset"

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


def build_inference_args(params: dict) -> list[str]:
    """웹 UI 파라미터를 wrapper CLI 인자 리스트로 변환."""
    args = [settings.local_python, "-u", WRAPPER_PATH]  # -u: unbuffered stdout

    # RTC/ACT 파라미터는 config-overrides로 묶어서 전달
    overrides = {}
    override_keys = {"max_guidance_weight", "execution_horizon", "temporal_ensemble_coeff", "n_action_steps", "refill_threshold_pct"}

    for key, value in params.items():
        if key in override_keys:
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
    """gRPC grpc_wrapper.py CLI 인자 빌더."""
    args = [settings.grpc_python, "-u", GRPC_WRAPPER_PATH]

    for key, value in params.items():
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


def build_train_args(params: dict) -> list[str]:
    """학습 CLI 인자 빌더."""
    args = [settings.grpc_python, "-m", "lerobot.scripts.lerobot_train"]

    # --policy.path와 --policy.type은 동시에 사용 불가
    has_pretrained = bool(params.get("pretrained_path"))

    for key, value in params.items():
        if has_pretrained and key == "policy_type":
            continue  # pretrained_path가 있으면 policy_type 스킵
        cli_flag = TRAIN_ARGS_MAP.get(key)
        if cli_flag is None:
            continue
        if isinstance(value, bool):
            if value:
                args.append(f"{cli_flag}=true")
            else:
                args.append(f"{cli_flag}=false")
        else:
            args.append(f"{cli_flag}={value}")

    # policy.push_to_hub는 기본 True → repo_id 없으면 validate()에서 에러.
    # 로컬 학습(policy_repo_id 미지정)이면 Hub 푸시를 명시적으로 비활성화
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

    # rename_map (카메라 이름 매핑)
    rename_map = params.get("rename_map", "")
    if not rename_map and has_pretrained:
        # pretrained 모델의 preprocessor에서 rename_map 자동 추출
        import json
        pre_path = Path(params["pretrained_path"]) / "policy_preprocessor.json"
        if pre_path.exists():
            try:
                pre_data = json.loads(pre_path.read_text())
                for step in pre_data.get("steps", []):
                    if step.get("registry_name") == "rename_observations_processor":
                        rm = step.get("config", {}).get("rename_map", {})
                        if rm:
                            rename_map = json.dumps(rm)
                            break
            except Exception:
                pass
    if rename_map:
        args.append(f"--rename_map={rename_map}")

    # state/action 차원 오버라이드: pretrained 모델의 config.json을 임시 수정
    state_dim = params.get("state_dim", 0)
    action_dim = params.get("action_dim", 0)
    if (state_dim or action_dim) and has_pretrained:
        import json as _json
        config_path = Path(params["pretrained_path"]) / "config.json"
        if config_path.exists():
            try:
                cfg = _json.loads(config_path.read_text())
                modified = False
                if state_dim and state_dim > 0:
                    if "observation.state" in cfg.get("input_features", {}):
                        cfg["input_features"]["observation.state"]["shape"] = [state_dim]
                        modified = True
                if action_dim and action_dim > 0:
                    if "action" in cfg.get("output_features", {}):
                        cfg["output_features"]["action"]["shape"] = [action_dim]
                        modified = True
                if modified:
                    config_path.write_text(_json.dumps(cfg, indent=2))
                    import logging
                    logging.getLogger(__name__).info("Updated config.json: state=%s action=%s", state_dim, action_dim)
            except Exception:
                pass

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
