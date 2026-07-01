"""
LeRobot 추론 래퍼 (v0.5+).

⚠️ CLI 래핑 원칙의 유일한 예외: policy 객체를 런타임에 수정해야 하므로 직접 import.
import 범위를 최소화하되, 실제 추론 루프 전체를 제어.

사용법:
  python lerobot_wrapper.py \
    --policy-path wego-hansu/piper_smolvla_teleop_033_E \
    --robot-type piper_follower \
    --robot-port can_follower1 \
    --cameras '{"top": {"type": "opencv", "index_or_path": 0, "fps": 30, "width": 640, "height": 480}}' \
    --fps 30 \
    --device cuda \
    --zmq-addr tcp://127.0.0.1:5555
"""

# ── lerobot.policies.__init__.py 실행 방지 ──
# groot 정책이 Python 3.13 dataclass와 비호환이므로
# 다른 어떤 lerobot import보다 먼저 더미 패키지를 등록
import sys
import types as _types

# lerobot 패키지 경로를 찾아서 __path__에 설정 (하위 모듈 import 가능하게)
import importlib as _il
import os as _os
_lerobot = _il.import_module("lerobot")
_policies_dir = _os.path.join(_os.path.dirname(_lerobot.__file__), "policies")

_pkg = _types.ModuleType("lerobot.policies")
_pkg.__path__ = [_policies_dir]
_pkg.__package__ = "lerobot.policies"
sys.modules["lerobot.policies"] = _pkg

# HuggingFace/transformers 오프라인 모드: 로컬 캐시만 사용
_os.environ["HF_HUB_OFFLINE"] = "1"
_os.environ["TRANSFORMERS_OFFLINE"] = "1"

import argparse
import importlib
import json
import logging
import signal
import threading
import time
from contextlib import nullcontext
from copy import copy
from pathlib import Path
from typing import Any

import numpy as np
import zmq

# root logger에 stdout handler만 설정 (중복 방지)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.root.handlers = [_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# ── 실시간 변경 가능 파라미터 ──

def _set_rtc(policy: Any, attr: str, value: Any) -> None:
    rtc = getattr(policy.config, "rtc_config", None)
    if rtc is None:
        logger.warning("rtc_config is None, skipping %s", attr)
        return
    setattr(rtc, attr, value)


PARAM_SETTERS = {
    "max_guidance_weight": lambda p, v: _set_rtc(p, "max_guidance_weight", float(v)),
    "execution_horizon": lambda p, v: _set_rtc(p, "execution_horizon", int(v)),
    "temporal_ensemble_coeff": lambda p, v: setattr(p.config, "temporal_ensemble_coeff", float(v)) if hasattr(p.config, "temporal_ensemble_coeff") else None,
    # n_action_steps는 policy.config가 아니라 _actions_per_chunk(큐 크기)로 별도 관리 → param_listener에서 처리
    "use_amp": lambda p, v: setattr(p.config, "use_amp", bool(v)),
}

# task 텍스트 (ZMQ로 실시간 변경 가능)
_current_task = "do the task"

# 액션 필터 (ZMQ로 실시간 변경 가능)
from action_filter import ActionFilter
_action_filter = ActionFilter()

# 일시정지 + 수동 조작
_paused = False
_manual_action: dict | None = None  # {"joint1.pos": 0.0, ...}

# 다음 추론 트리거 시점: 액션 큐 잔량이 이 % 이하로 떨어지면 재추론.
#   0   = 큐가 완전히 빌 때만 (지연 동안 정지 가능)
#   100 = 매 스텝 재추론
_refill_threshold_pct = 20.0

# 추론 1회에서 받아 큐에 넣는 액션 수 (모델 chunk_size 이하로 자동 클램핑).
# 시작 시 확정하되 ZMQ n_action_steps로 런타임 변경 가능. 0 = 모델 전체 청크.
_actions_per_chunk = 0

# 정책 타입별 lazy import (policies/__init__.py 우회)
POLICY_IMPORTS = {
    "smolvla": ("lerobot.policies.smolvla.modeling_smolvla", "SmolVLAPolicy",
                "lerobot.policies.smolvla.configuration_smolvla", "SmolVLAConfig"),
    "act": ("lerobot.policies.act.modeling_act", "ACTPolicy",
            "lerobot.policies.act.configuration_act", "ACTConfig"),
    "diffusion": ("lerobot.policies.diffusion.modeling_diffusion", "DiffusionPolicy",
                  "lerobot.policies.diffusion.configuration_diffusion", "DiffusionConfig"),
    "pi0": ("lerobot.policies.pi0.modeling_pi0", "PI0Policy",
            "lerobot.policies.pi0.configuration_pi0", "PI0Config"),
    "pi05": ("lerobot.policies.pi05.modeling_pi05", "PI05Policy",
             "lerobot.policies.pi05.configuration_pi05", "PI05Config"),
    "pi0_fast": ("lerobot.policies.pi0_fast.modeling_pi0_fast", "PI0FastPolicy",
                 "lerobot.policies.pi0_fast.configuration_pi0_fast", "PI0FastConfig"),
    "tdmpc": ("lerobot.policies.tdmpc.modeling_tdmpc", "TDMPCPolicy",
              "lerobot.policies.tdmpc.configuration_tdmpc", "TDMPCConfig"),
    "vqbet": ("lerobot.policies.vqbet.modeling_vqbet", "VQBeTPolicy",
              "lerobot.policies.vqbet.configuration_vqbet", "VQBeTConfig"),
}


def param_listener(policy: Any, zmq_addr: str) -> None:
    """ZMQ PULL 소켓에서 파라미터를 수신하여 policy 객체에 실시간 반영."""
    global _current_task, _paused, _manual_action, _refill_threshold_pct, _actions_per_chunk
    ctx = zmq.Context()
    sock = ctx.socket(zmq.PULL)
    sock.bind(zmq_addr)
    logger.info("ZMQ param listener bound to %s", zmq_addr)

    while True:
        try:
            msg = sock.recv_json()
            for key, value in msg.items():
                # 일시정지/재개
                if key == "pause":
                    _paused = bool(value)
                    logger.info("Inference %s", "PAUSED" if _paused else "RESUMED")
                    continue
                # 수동 관절 조작
                if key == "manual_action":
                    _manual_action = value if isinstance(value, dict) else None
                    continue
                # task 변경
                if key == "task":
                    _current_task = str(value)
                    logger.info("Updated task = %s", _current_task)
                    continue
                # 재추론 트리거 임계치 (큐 잔량 %)
                if key == "refill_threshold_pct":
                    _refill_threshold_pct = max(0.0, min(100.0, float(value)))
                    logger.info("Updated refill_threshold_pct = %s", _refill_threshold_pct)
                    continue
                # 받아오는 액션 청크 크기 (실제 슬라이싱은 모델 chunk_size로 클램핑)
                #   use_chunk_size: 0=모델 전체, N=앞 N개만 사용 (UI "액션 청크 크기" 슬라이더)
                #   n_action_steps: ACT 파라미터 슬라이더 (동일하게 청크 크기로 처리)
                if key in ("use_chunk_size", "n_action_steps"):
                    _actions_per_chunk = max(0, int(value)) if key == "use_chunk_size" else max(1, int(value))
                    logger.info("Updated actions_per_chunk = %d (via %s)", _actions_per_chunk, key)
                    continue
                # 액션 필터 파라미터
                if _action_filter.update_param(key, value):
                    logger.info("Updated filter %s = %s", key, value)
                    continue
                setter = PARAM_SETTERS.get(key)
                if setter:
                    try:
                        setter(policy, value)
                        logger.info("Updated %s = %s", key, value)
                    except Exception as e:
                        logger.error("Failed to set %s: %s", key, e)
                else:
                    logger.warning("Unknown param: %s", key)
        except Exception as e:
            logger.error("ZMQ recv error: %s", e)


def _resolve_policy_path(policy_path: str) -> str:
    """HF repo ID 또는 로컬 경로를 실제 디렉토리 경로로 변환."""
    if _os.path.isdir(policy_path):
        return policy_path
    # HF 캐시에서 찾기
    from huggingface_hub import snapshot_download
    return snapshot_download(repo_id=policy_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="LeRobot inference wrapper (v0.5+)")
    parser.add_argument("--policy-path", required=True)
    parser.add_argument("--robot-type", required=True)
    parser.add_argument("--robot-port", required=True)
    parser.add_argument("--cameras", default="{}")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--use-amp", action="store_true")
    parser.add_argument("--task", default=None)
    parser.add_argument("--zmq-addr", default="tcp://127.0.0.1:5555")
    parser.add_argument("--config-overrides", default="{}")
    parser.add_argument("--debug", action="store_true", help="주고받은 모든 데이터를 별도 폴더에 기록")
    args = parser.parse_args()

    cameras_cfg = json.loads(args.cameras)
    config_overrides = json.loads(args.config_overrides)

    # ── LeRobot import ──
    try:
        import torch
        from lerobot.robots import make_robot_from_config
        from lerobot.utils.import_utils import register_third_party_plugins
        from lerobot.utils.utils import get_safe_torch_device
    except ImportError as e:
        logger.error("LeRobot 또는 의존성 누락: %s", e)
        raise

    register_third_party_plugins()

    global _current_task, _refill_threshold_pct, _actions_per_chunk
    _current_task = args.task if args.task else "do the task"

    # ── 1. 로봇 생성 ──
    logger.info("Creating robot: %s (port=%s, cameras=%d)", args.robot_type, args.robot_port, len(cameras_cfg))

    from lerobot.robots.config import RobotConfig
    RobotCfgClass = RobotConfig.get_choice_class(args.robot_type)
    robot_cfg = RobotCfgClass(port=args.robot_port)

    if cameras_cfg and hasattr(robot_cfg, "cameras"):
        from lerobot.cameras.configs import CameraConfig
        # 카메라 서브클래스 등록을 위한 import
        try:
            from lerobot.cameras.opencv.configuration_opencv import OpenCVCameraConfig  # noqa: F401
        except ImportError:
            pass
        try:
            from lerobot.cameras.realsense.configuration_realsense import RealSenseCameraConfig  # noqa: F401
        except ImportError:
            pass
        for cam_name, cam_params in cameras_cfg.items():
            cam_type = cam_params.pop("type", "opencv")
            # OpenCV 카메라: V4L2 백엔드 강제 (ANY 사용 시 GStreamer 등에서 실패하는 경우 방지)
            if cam_type == "opencv" and "backend" not in cam_params:
                cam_params["backend"] = 200  # Cv2Backends.V4L2
            CamCfgClass = CameraConfig.get_choice_class(cam_type)
            robot_cfg.cameras[cam_name] = CamCfgClass(**cam_params)

    robot = make_robot_from_config(robot_cfg)
    robot.connect()
    logger.info("Robot connected: %s (%d motors)", robot.name, len(robot.action_features))

    # ── 2. 정책 로드 ──
    local_path = _resolve_policy_path(args.policy_path)
    logger.info("Loading policy from: %s", local_path)

    # config.json에서 type 필드 읽기
    config_json = json.loads((Path(local_path) / "config.json").read_text())
    policy_type = config_json.get("type", "")

    if policy_type not in POLICY_IMPORTS:
        raise ValueError(f"Unsupported policy type: '{policy_type}'. Supported: {list(POLICY_IMPORTS.keys())}")

    model_mod, model_cls, config_mod, config_cls = POLICY_IMPORTS[policy_type]

    # policy 클래스 import & from_pretrained (서버 방식: 모델 자체 config 사용)
    PolicyClass = getattr(importlib.import_module(model_mod), model_cls)
    logger.info("Loading %s", model_cls)
    policy = PolicyClass.from_pretrained(local_path)
    policy_cfg = policy.config

    device = get_safe_torch_device(args.device)
    policy_cfg.device = args.device
    policy_cfg.use_amp = args.use_amp
    policy.eval()
    policy.to(device)

    # 시각화 hooks 등록 (input/features/attention 이미지 생성)
    try:
        from viz_hooks import register_viz_hooks
        register_viz_hooks(policy)
        logger.info("Viz hooks registered")
    except Exception as e:
        logger.warning("Viz hooks registration failed: %s", e)

    # config 오버라이드 적용 (n_action_steps는 policy.config에 적용하지 않음 — actions_per_chunk로 별도 관리)
    _actions_per_chunk_override = None
    for key, value in config_overrides.items():
        if key == "n_action_steps":
            _actions_per_chunk_override = int(value)
            logger.info("actions_per_chunk override: %s", value)
            continue
        if key == "refill_threshold_pct":
            _refill_threshold_pct = max(0.0, min(100.0, float(value)))
            logger.info("refill_threshold_pct override: %s", _refill_threshold_pct)
            continue
        setter = PARAM_SETTERS.get(key)
        if setter:
            setter(policy, value)
            logger.info("Config override: %s = %s", key, value)

    # ── 3. 전처리/후처리 파이프라인 ──
    preprocessor = None
    postprocessor = None
    try:
        # SmolVLA 등 정책별 프로세서 레지스트리 등록
        for _proc_mod in [
            "lerobot.policies.smolvla.processor_smolvla",
            "lerobot.policies.pi0.processor_pi0",
            "lerobot.policies.pi05.processor_pi05",
        ]:
            try:
                importlib.import_module(_proc_mod)
            except Exception:
                pass
        from lerobot.policies.factory import make_pre_post_processors
        preprocessor, postprocessor = make_pre_post_processors(
            policy_cfg,
            pretrained_path=local_path,
            preprocessor_overrides={"device_processor": {"device": args.device}},
            postprocessor_overrides={"device_processor": {"device": "cpu"}},
        )
        logger.info("Preprocessor/Postprocessor loaded via make_pre_post_processors")
    except Exception as e:
        logger.warning("Failed to load pre/post processors: %s (falling back to manual)", e)

    # preprocessor의 rename_map 추출 (fallback용)
    _rename_map = {}
    try:
        pre_json_path = Path(local_path) / "policy_preprocessor.json"
        if pre_json_path.exists():
            import json as _json
            for step in _json.loads(pre_json_path.read_text()).get("steps", []):
                if step.get("registry_name") == "rename_observations_processor":
                    _rename_map = step.get("config", {}).get("rename_map", {})
                    break
        if _rename_map:
            logger.info("Rename map: %s", _rename_map)
    except Exception:
        pass

    # ── 4. ZMQ 파라미터 리스너 시작 ──
    zmq_thread = threading.Thread(target=param_listener, args=(policy, args.zmq_addr), daemon=True)
    zmq_thread.start()

    # ── 5. 추론 루프 ──
    running = True

    def signal_handler(sig, _):
        nonlocal running
        logger.info("Signal %s received, stopping...", sig)
        running = False

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    target_dt = 1.0 / args.fps
    step = 0
    _fps_history: list[float] = []
    _action_filter.fps = args.fps

    # 토크나이저 + 인코딩 캐시 (매 스텝 재호출 방지)
    _tokenizer = None
    _max_token_len = getattr(policy_cfg, 'tokenizer_max_length', 48)
    _cached_task = None
    _cached_tokens = None
    _cached_mask = None
    if hasattr(policy, 'model') and hasattr(policy.model, 'vlm_with_expert'):
        _tokenizer = policy.model.vlm_with_expert.processor.tokenizer

    logger.info("Starting inference loop (fps=%d, device=%s, amp=%s)", args.fps, args.device, args.use_amp)

    # ── CSV 로그 ──
    import csv
    import datetime as _dt
    _log_ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    _csv_path = f"/tmp/piper_inference_{_log_ts}.csv"
    _csv_motor_names = list(robot.action_features.keys())
    _csv_cols = (
        ["timestamp", "step", "fps", "inference_ms", "queue_size"]
        + [f"target_{m}" for m in _csv_motor_names]
        + [f"filtered_{m}" for m in _csv_motor_names]
        + [f"actual_{m}" for m in _csv_motor_names]
        + ["task", "paused"]
    )
    _csv_file = open(_csv_path, "w", newline="")
    _csv_writer = csv.DictWriter(_csv_file, fieldnames=_csv_cols)
    _csv_writer.writeheader()
    logger.info("CSV log: %s", _csv_path)

    # ── 디버그 레코더 (--debug 시 주고받은 모든 데이터 기록) ──
    recorder = None
    if args.debug:
        try:
            from debug_recorder import DebugRecorder
            recorder = DebugRecorder("local", {
                "policy_path": args.policy_path,
                "robot_type": args.robot_type,
                "robot_port": args.robot_port,
                "fps": args.fps,
                "device": args.device,
                "motor_names": _csv_motor_names,
                "cameras": list(cameras_cfg.keys()),
            })
        except Exception as e:
            logger.error("Debug recorder init failed: %s", e)

    # ── obs 변환용 LeRobot 유틸리티 (이전 서버 방식과 동일) ──
    from lerobot.datasets.utils import build_dataset_frame, hw_to_dataset_features

    lerobot_features = hw_to_dataset_features(robot.observation_features, "observation", use_video=False)
    policy_image_features = policy.config.image_features
    _actions_per_chunk = _actions_per_chunk_override or policy_cfg.n_action_steps
    logger.info("actions_per_chunk: %d (model default: %d)", _actions_per_chunk, policy_cfg.n_action_steps)

    def _prepare_observation(raw_obs: dict) -> dict:
        """raw_obs → 모델 입력 변환. preprocessor가 있으면 사용."""
        OBS_STATE = "observation.state"
        OBS_IMAGES = "observation.images."

        # 1. build_dataset_frame으로 키 매핑
        lerobot_obs = build_dataset_frame(lerobot_features, raw_obs, prefix="observation")

        # 2. rename_map 적용 (side→camera1 등)
        if _rename_map:
            renamed = {}
            for key, val in lerobot_obs.items():
                new_key = _rename_map.get(key, key)
                renamed[new_key] = val
            lerobot_obs = renamed

        # 3. state를 모델 기대 차원으로 트림 (학습 시 그리퍼 제외된 경우)
        _state_dim = policy_cfg.input_features.get("observation.state", {})
        _expected_state_len = _state_dim.shape[0] if hasattr(_state_dim, 'shape') else 0
        if _expected_state_len > 0:
            obs_state = lerobot_obs.get("observation.state")
            if obs_state is not None and hasattr(obs_state, '__len__') and len(obs_state) > _expected_state_len:
                lerobot_obs["observation.state"] = obs_state[:_expected_state_len]

        if preprocessor is not None:
            # preprocessor 파이프라인 사용 (정규화, 토큰화, 디바이스 이동 포함)
            # 이미지를 (C, H, W) float32 [0,1] 텐서로 변환
            result = {}
            for key, val in lerobot_obs.items():
                if key.startswith(OBS_IMAGES):
                    img = torch.tensor(val).permute(2, 0, 1).float() / 255.0
                    result[key] = img
                elif key == OBS_STATE:
                    result[key] = torch.tensor(val)
                else:
                    result[key] = val
            result["task"] = _current_task
            # preprocessor가 batch dim 추가, 토큰화, 정규화, 디바이스 이동을 처리
            result = preprocessor(result)
            return result

        # fallback: preprocessor 없는 이전 모델
        state = torch.tensor(lerobot_obs[OBS_STATE])
        if state.ndim == 1:
            state = state.unsqueeze(0)
        result = {OBS_STATE: state.to(device)}

        for key in list(lerobot_obs.keys()):
            if key.startswith(OBS_IMAGES):
                img = torch.tensor(lerobot_obs[key])
                img = img.permute(2, 0, 1)  # (H,W,C) → (C,H,W)
                # リサイズしない — prepare_images()が512x512にリサイズ+パディングする
                img = img.float() / 255.0
                img = img.contiguous()
                result[key] = img.unsqueeze(0).to(device)

        task_text = _current_task
        result["task"] = task_text

        if _tokenizer is not None:
            nonlocal _cached_task, _cached_tokens, _cached_mask
            if task_text != _cached_task:
                encoded = _tokenizer(
                    task_text, padding="max_length",
                    max_length=_max_token_len, truncation=True,
                    return_tensors="pt",
                )
                _cached_tokens = encoded["input_ids"].to(device)
                _cached_mask = encoded["attention_mask"].bool().to(device)
                _cached_task = task_text
            result["observation.language.tokens"] = _cached_tokens
            result["observation.language.attention_mask"] = _cached_mask

        return result

    # ── 비동기 추론: 공유 상태 ──
    _action_lock = threading.Lock()
    _latest_action_dict: dict | None = None
    _latest_action_np: np.ndarray | None = None
    _action_queue: list = []  # 타임스텝 기반 액션 큐
    _obs_for_inference: dict | None = None
    _obs_event = threading.Event()
    _inference_running = True
    _inference_ms_shared = 0.0
    _infer_seq = 0
    motor_names = list(robot.action_features.keys())

    def _inference_thread_fn():
        """별도 스레드에서 predict_action_chunk() 실행 (서버 방식)."""
        nonlocal _latest_action_dict, _latest_action_np, _inference_ms_shared, _action_queue, _infer_seq
        while _inference_running:
            triggered = _obs_event.wait(timeout=1.0)
            if not _inference_running:
                break
            # 타임아웃(새 관측 없음)이면 재추론 금지 — 낡은 obs로 큐를 채워
            # 메인 루프의 큐-소진 재추론(_obs_for_inference 갱신)을 막는 것을 방지.
            if not triggered:
                continue
            _obs_event.clear()

            raw_obs = _obs_for_inference
            if raw_obs is None:
                continue

            seq = _infer_seq
            _infer_seq += 1
            if recorder is not None:
                recorder.record_observation(seq, raw_obs, _current_task)

            t0 = time.monotonic()
            try:
                with (
                    torch.inference_mode(),
                    torch.autocast(device_type=device.type) if device.type == "cuda" and policy.config.use_amp else nullcontext(),
                ):
                    observation = _prepare_observation(raw_obs)
                    # 시각화용: _captured에 텐서 저장 (features/attention 생성용)
                    try:
                        from viz_hooks import _captured
                        viz_imgs = []
                        for k, v in raw_obs.items():
                            if isinstance(v, np.ndarray) and v.ndim == 3:
                                t = torch.tensor(v.copy()).permute(2, 0, 1).float() / 255.0
                                viz_imgs.append(t)
                        if viz_imgs:
                            _captured["preprocessed_images"] = viz_imgs
                    except Exception:
                        pass
                    # predict_action_chunk — 서버와 동일
                    action_chunk = policy.predict_action_chunk(observation)
                    if action_chunk.ndim != 3:
                        action_chunk = action_chunk.unsqueeze(0)
                    # (B, chunk_size, action_dim) → (n, action_dim).
                    # 원하는 크기가 모델 실제 청크 길이를 넘지 않도록 클램핑 (0=전체).
                    _model_chunk = action_chunk.shape[1]
                    _apc = _model_chunk if _actions_per_chunk <= 0 else max(1, min(_actions_per_chunk, _model_chunk))
                    action_chunk = action_chunk[0, :_apc, :]

                # postprocessor 적용 (unnormalize) + 액션 큐에 추가
                new_queue = []
                for i in range(action_chunk.shape[0]):
                    action_i = action_chunk[i].unsqueeze(0)  # (1, action_dim)
                    if postprocessor is not None:
                        action_i = postprocessor(action_i)
                    a_np = action_i.squeeze(0).cpu().numpy()
                    a_dict = {name: float(a_np[j]) for j, name in enumerate(motor_names)}
                    new_queue.append((a_np, a_dict))

                with _action_lock:
                    _action_queue = new_queue
                    # 첫 액션을 latest로 설정
                    if new_queue:
                        _latest_action_np, _latest_action_dict = new_queue[0]

                _inference_ms_shared = round((time.monotonic() - t0) * 1000, 1)

                if recorder is not None:
                    recorder.record_inference(
                        seq,
                        action_chunk.cpu().numpy(),
                        [a_dict for (_, a_dict) in new_queue],
                        _inference_ms_shared,
                    )
            except Exception as e:
                logger.error("Inference thread error: %s", e, exc_info=True)

    inference_thread = threading.Thread(target=_inference_thread_fn, daemon=True)
    inference_thread.start()

    try:
        while running:
            start_time = time.monotonic()

            # 일시정지 중: 수동 액션만 처리
            if _paused:
                if _manual_action:
                    robot.send_action(_manual_action)
                obs = robot.get_observation()
                state_values = [float(v) for k, v in obs.items() if isinstance(v, (int, float))]
                telemetry = {
                    "t": "telemetry", "step": step, "fps": 0,
                    "inference_ms": 0, "joints": [round(v, 2) for v in state_values],
                    "action": [], "task": _current_task, "paused": True,
                }
                print(json.dumps(telemetry), flush=True)
                time.sleep(0.05)
                continue

            # 5a. 관측값 가져오기
            obs = robot.get_observation()

            # 시각화: 메인 루프에서 직접 raw 이미지 저장 (obs 원래 순서 유지)
            try:
                from viz_hooks import _save_jpg
                import cv2 as _cv2
                _viz_i = 0
                for _k, _v in obs.items():
                    if isinstance(_v, np.ndarray) and _v.ndim == 3:
                        _save_jpg(_cv2.cvtColor(_v, _cv2.COLOR_RGB2BGR), f"piper_viz_input_{_viz_i}.jpg")
                        _viz_i += 1
            except Exception:
                pass

            if step == 0:
                logger.info("Observation keys: %s", list(obs.keys()))
                # 첫 추론 트리거
                _obs_for_inference = {k: v.copy() if isinstance(v, np.ndarray) else v for k, v in obs.items()}
                _obs_event.set()

            # 5b. 관절값 추출 (텔레메트리용)
            state_values = [float(v) for k, v in obs.items() if isinstance(v, (int, float))]

            # 5c. 액션 큐에서 순서대로 꺼내서 로봇에 전송
            with _action_lock:
                if _action_queue:
                    action_np, action_dict = _action_queue.pop(0)
                    _latest_action_np = action_np
                    _latest_action_dict = action_dict
                else:
                    action_dict = _latest_action_dict
                    action_np = _latest_action_np
                qsize_after = len(_action_queue)

            # 큐 잔량이 임계치(%) 이하로 떨어질 때만 새 추론을 트리거 (매 스텝 X).
            # 임계치를 높이면 큐가 비기 전에 미리 재추론해 지연을 감추고(액션 일부 폐기),
            # 낮추면 청크를 더 소진한 뒤 재추론(폐기 최소화, 지연 노출 위험).
            _refill_lead = max(1, _actions_per_chunk) * (_refill_threshold_pct / 100.0)
            if qsize_after <= _refill_lead and not _obs_event.is_set():
                _obs_for_inference = {k: v.copy() if isinstance(v, np.ndarray) else v for k, v in obs.items()}
                _obs_event.set()

            filtered = None
            if action_dict is not None:
                # 현재 관절 상태 (속도 제한용)
                current_state = {k: v for k, v in obs.items() if isinstance(v, (int, float))}
                filtered = _action_filter.apply(action_dict, current_state)
                robot.send_action(filtered)

            step += 1
            elapsed = time.monotonic() - start_time
            # FPS: 최근 20스텝 이동평균
            _fps_history.append(elapsed)
            if len(_fps_history) > 20:
                _fps_history.pop(0)
            current_fps = round(len(_fps_history) / max(sum(_fps_history), 1e-6), 1)

            # 텔레메트리
            telemetry = {
                "t": "telemetry",
                "step": step,
                "fps": current_fps,
                "inference_ms": _inference_ms_shared,
                "joints": [round(v, 2) for v in state_values],
                "action": [round(float(action_np[i]), 2) for i in range(len(action_np))] if action_np is not None else [],
                "task": _current_task,
            }
            if step % 50 == 0 and torch.cuda.is_available():
                telemetry["gpu_mem_mb"] = round(torch.cuda.memory_allocated() / 1024 / 1024)
                telemetry["gpu_total_mb"] = round(torch.cuda.get_device_properties(0).total_memory / 1024 / 1024)
            print(json.dumps(telemetry), flush=True)

            # CSV 로그
            with _action_lock:
                qsize = len(_action_queue)
            csv_row = {
                "timestamp": time.monotonic(),
                "step": step, "fps": current_fps,
                "inference_ms": _inference_ms_shared,
                "queue_size": qsize,
                "task": _current_task, "paused": _paused,
            }
            for j, m in enumerate(_csv_motor_names):
                csv_row[f"target_{m}"] = round(float(action_np[j]), 4) if action_np is not None else ""
                csv_row[f"filtered_{m}"] = round(filtered.get(m, 0), 4) if filtered else ""
                csv_row[f"actual_{m}"] = round(state_values[j], 4) if j < len(state_values) else ""
            _csv_writer.writerow(csv_row)
            if step % 30 == 0:
                _csv_file.flush()

            if recorder is not None:
                recorder.record_step(
                    step,
                    action_dict,
                    filtered,
                    {m: round(state_values[j], 4) for j, m in enumerate(_csv_motor_names) if j < len(state_values)},
                    _current_task,
                    _paused,
                    current_fps,
                )

            # 카메라 프리뷰 (20스텝마다)
            if step % 20 == 0:
                def _save_previews(snap):
                    import cv2 as _cv2
                    import tempfile as _tf
                    for cn, ci in snap.items():
                        if isinstance(ci, np.ndarray) and ci.ndim >= 2:
                            pp = f"/tmp/piper_cam_{cn}.jpg"
                            fd, tp = _tf.mkstemp(suffix=".jpg", dir="/tmp")
                            try: _cv2.imwrite(tp, _cv2.cvtColor(ci, _cv2.COLOR_RGB2BGR)); _os.replace(tp, pp)
                            except Exception: pass
                            finally:
                                try: _os.close(fd)
                                except OSError: pass
                threading.Thread(target=_save_previews, args=({k: v for k, v in obs.items()},), daemon=True).start()

            # FPS 제어
            if elapsed < target_dt:
                time.sleep(target_dt - elapsed)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error("Inference error: %s", e, exc_info=True)
    finally:
        _inference_running = False
        _obs_event.set()
        inference_thread.join(timeout=3)
        logger.info("Returning to home position...")
        try:
            robot.parking()  # motion_status를 폴링하며 팔이 멈출 때까지 이미 블로킹함
            time.sleep(0.5)  # 토크 차단 전 최종 정지 안정화 여유 (parking이 모션 완료 보장)
        except Exception as e:
            logger.warning("Parking failed: %s", e)
        logger.info("Disabling torque and disconnecting...")
        try:
            robot.disconnect(disable_torque=True)
        except Exception:
            pass
        _csv_file.close()
        _debug_dir = recorder.close() if recorder is not None else None
        print(json.dumps({"t": "log_saved", "csv_path": _csv_path, "steps": step, "debug_dir": _debug_dir}), flush=True)
        logger.info("Done. Total steps: %d, CSV log: %s%s", step, _csv_path,
                    f", debug: {_debug_dir}" if _debug_dir else "")


if __name__ == "__main__":
    main()
