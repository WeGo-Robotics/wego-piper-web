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

import argparse
import importlib
import json
import logging
import os
import signal
import threading
import time
from contextlib import nullcontext
from copy import copy
from pathlib import Path
from typing import Any

import numpy as np
import zmq

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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
    "n_action_steps": lambda p, v: setattr(p.config, "n_action_steps", int(v)),
    "use_amp": lambda p, v: setattr(p.config, "use_amp", bool(v)),
}

# task 텍스트 (ZMQ로 실시간 변경 가능)
_current_task = "do the task"

# 일시정지 + 수동 조작
_paused = False
_manual_action: dict | None = None  # {"joint1.pos": 0.0, ...}

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
    global _current_task, _paused, _manual_action
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
    if os.path.isdir(policy_path):
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

    global _current_task
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

    # config 서브클래스 import & JSON에서 직접 생성
    ConfigClass = getattr(importlib.import_module(config_mod), config_cls)
    import draccus
    # "type"은 draccus choice discriminator이지 dataclass 필드가 아니므로 제거
    config_data = {k: v for k, v in config_json.items() if k != "type"}
    policy_cfg = draccus.decode(ConfigClass, config_data)
    policy_cfg.device = args.device
    policy_cfg.use_amp = args.use_amp

    # policy 클래스 import & from_pretrained
    PolicyClass = getattr(importlib.import_module(model_mod), model_cls)
    logger.info("Loading %s", model_cls)
    policy = PolicyClass.from_pretrained(local_path, config=policy_cfg)

    device = get_safe_torch_device(args.device)
    policy.eval()
    policy.to(device)

    # config 오버라이드 적용
    for key, value in config_overrides.items():
        setter = PARAM_SETTERS.get(key)
        if setter:
            setter(policy, value)
            logger.info("Config override: %s = %s", key, value)

    # ── 3. 전처리/후처리 파이프라인 ──
    from lerobot.processor import PolicyProcessorPipeline

    pre_path = Path(local_path) / "preprocessor.json"
    post_path = Path(local_path) / "postprocessor.json"
    preprocessor = PolicyProcessorPipeline.from_json(str(pre_path)) if pre_path.exists() else PolicyProcessorPipeline()
    postprocessor = PolicyProcessorPipeline.from_json(str(post_path)) if post_path.exists() else PolicyProcessorPipeline()

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

    logger.info("Starting inference loop (fps=%d, device=%s, amp=%s)", args.fps, args.device, args.use_amp)

    try:
        while running:
            start_time = time.monotonic()

            # 일시정지 중: 수동 액션만 처리
            if _paused:
                if _manual_action:
                    robot.send_action(_manual_action)
                # 일시정지 상태 텔레메트리
                obs = robot.get_observation()
                state_values = [float(v) for k, v in obs.items()
                                if isinstance(v, (int, float))]
                telemetry = {
                    "t": "telemetry", "step": step, "fps": 0,
                    "inference_ms": 0, "joints": [round(v, 2) for v in state_values],
                    "action": [], "task": _current_task, "paused": True,
                }
                print(json.dumps(telemetry), flush=True)
                # 카메라 프리뷰는 계속 갱신
                import cv2 as _cv2
                import tempfile
                for cam_name, cam_img in obs.items():
                    if isinstance(cam_img, np.ndarray) and cam_img.ndim >= 2:
                        preview_path = f"/tmp/piper_cam_{cam_name}.jpg"
                        fd, tmp_path = tempfile.mkstemp(suffix=".jpg", dir="/tmp")
                        try:
                            _cv2.imwrite(tmp_path, cam_img)
                            os.replace(tmp_path, preview_path)
                        except Exception:
                            pass
                        finally:
                            try: os.close(fd)
                            except OSError: pass
                time.sleep(0.05)
                continue

            # 5a. 관측값 가져오기
            obs = robot.get_observation()

            # 디버그: 첫 스텝에서 observation 키 출력
            if step == 0:
                logger.info("Observation keys: %s", list(obs.keys()))
                for k, v in obs.items():
                    if hasattr(v, 'shape'):
                        logger.info("  %s: %s %s", k, type(v).__name__, v.shape)
                    else:
                        logger.info("  %s: %s %s", k, type(v).__name__, v)

            # 5b. observation 키를 정책이 기대하는 형태로 변환
            # robot: "joint1.pos" → "observation.state", "top" → "observation.images.top"
            mapped_obs: dict = {}
            state_values = []
            for k, v in obs.items():
                if isinstance(v, np.ndarray) and v.ndim >= 2:
                    # 이미지 → observation.images.{name}
                    mapped_obs[f"observation.images.{k}"] = v
                elif isinstance(v, np.ndarray) or isinstance(v, (int, float)):
                    # 관절값 → state에 추가
                    state_values.append(float(v) if isinstance(v, (int, float)) else v.item() if v.ndim == 0 else v)

            # state를 하나의 벡터로 합침
            if state_values:
                mapped_obs["observation.state"] = np.array(state_values, dtype=np.float32)

            _obs = copy(mapped_obs)
            with (
                torch.inference_mode(),
                torch.autocast(device_type=device.type) if device.type == "cuda" and policy.config.use_amp else nullcontext(),
            ):
                for name in list(_obs.keys()):
                    val = _obs[name]
                    if isinstance(val, np.ndarray):
                        val = torch.from_numpy(val)
                    elif isinstance(val, (int, float)):
                        val = torch.tensor([val])
                    else:
                        continue
                    if "image" in name:
                        val = val.float() / 255.0
                        val = val.permute(2, 0, 1).contiguous()
                    _obs[name] = val.unsqueeze(0).to(device)

                # SmolVLA 등 VLA 모델은 task 텍스트를 토크나이즈하여 전달
                task_text = _current_task
                _obs["task"] = task_text
                _obs["robot_type"] = args.robot_type

                # observation.language.tokens / attention_mask 생성
                if hasattr(policy, 'model') and hasattr(policy.model, 'vlm_with_expert'):
                    tokenizer = policy.model.vlm_with_expert.processor.tokenizer
                    max_len = policy_cfg.tokenizer_max_length if hasattr(policy_cfg, 'tokenizer_max_length') else 48
                    encoded = tokenizer(
                        task_text,
                        padding="max_length",
                        max_length=max_len,
                        truncation=True,
                        return_tensors="pt",
                    )
                    _obs["observation.language.tokens"] = encoded["input_ids"].to(device)
                    _obs["observation.language.attention_mask"] = encoded["attention_mask"].bool().to(device)

                _obs = preprocessor(_obs)
                action = policy.select_action(_obs)
                # postprocessor가 비어있으면 Tensor를 직접 반환하므로 건너뜀
                if len(postprocessor.steps) > 0:
                    action = postprocessor(action)

            # 5c. 액션 Tensor → dict 변환 후 로봇에 전송
            if isinstance(action, torch.Tensor):
                action_np = action.squeeze(0).cpu().numpy()
                motor_names = list(robot.action_features.keys())
                action_dict = {name: float(action_np[i]) for i, name in enumerate(motor_names)}
            else:
                action_dict = action
            robot.send_action(action_dict)

            step += 1
            elapsed = time.monotonic() - start_time
            inference_ms = round(elapsed * 1000, 1)
            current_fps = round(1.0 / max(elapsed, 1e-6), 1)

            # 텔레메트리 + 카메라 프리뷰 (5스텝마다 ≈ 추론 2~3회에 1번)
            if step % 5 == 0:
                telemetry = {
                    "t": "telemetry",
                    "step": step,
                    "fps": current_fps,
                    "inference_ms": inference_ms,
                    "joints": [round(v, 2) for v in state_values],
                    "action": [round(float(action_np[i]), 2) for i in range(len(action_np))] if isinstance(action, torch.Tensor) else [],
                    "task": task_text,
                }
                if step % 50 == 0 and torch.cuda.is_available():
                    telemetry["gpu_mem_mb"] = round(torch.cuda.memory_allocated() / 1024 / 1024)
                    telemetry["gpu_total_mb"] = round(torch.cuda.get_device_properties(0).total_memory / 1024 / 1024)
                print(json.dumps(telemetry), flush=True)

            # 카메라 프리뷰 (20스텝마다)
            if step % 20 == 0:
                import cv2 as _cv2
                import tempfile
                for cam_name, cam_img in obs.items():
                    if isinstance(cam_img, np.ndarray) and cam_img.ndim >= 2:
                        preview_path = f"/tmp/piper_cam_{cam_name}.jpg"
                        fd, tmp_path = tempfile.mkstemp(suffix=".jpg", dir="/tmp")
                        try:
                            _cv2.imwrite(tmp_path, cam_img)
                            os.replace(tmp_path, preview_path)
                        except Exception:
                            pass
                        finally:
                            try: os.close(fd)
                            except OSError: pass

            # FPS 제어
            if elapsed < target_dt:
                time.sleep(target_dt - elapsed)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error("Inference error: %s", e, exc_info=True)
    finally:
        logger.info("Returning to home position...")
        try:
            robot.parking()
            time.sleep(1)
        except Exception as e:
            logger.warning("Parking failed: %s", e)
        logger.info("Disabling torque and disconnecting...")
        try:
            robot.disconnect(disable_torque=True)
        except Exception:
            pass
        logger.info("Done. Total steps: %d", step)


if __name__ == "__main__":
    main()
