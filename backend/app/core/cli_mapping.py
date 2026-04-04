"""
LeRobot CLI 인자 매핑 테이블.
LeRobot 업데이트 시 이 파일만 수정하면 됨.
"""

from pathlib import Path

# 추론 CLI 명령어 (LeRobot v0.5: lerobot-eval은 시뮬, 실제 로봇은 wrapper 사용)
INFERENCE_CMD = "python"
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
    args = [INFERENCE_CMD, "-u", WRAPPER_PATH]  # -u: unbuffered stdout

    # RTC/ACT 파라미터는 config-overrides로 묶어서 전달
    overrides = {}
    override_keys = {"max_guidance_weight", "execution_horizon", "temporal_ensemble_coeff", "n_action_steps"}

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
