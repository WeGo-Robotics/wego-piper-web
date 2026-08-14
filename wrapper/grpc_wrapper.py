"""
gRPC 추론 래퍼 (lerobot 0.5+ / python3.13) — 원격 정책 서버 + 웹 UI 연동.

사용법:
  python grpc_wrapper.py \
    --server-address 127.0.0.1:8088 \
    --robot-type piper_follower \
    --robot-port can_follower1 \
    --cameras '{"top": {"type": "opencv", "index_or_path": 12}, "hand": {"type": "opencv", "index_or_path": 6}}' \
    --pretrained-path wego-hansu/piper_smolvla_teleop_033_D \
    --policy-type smolvla \
    --policy-device cuda \
    --actions-per-chunk 50 \
    --task "Pick the car and put in the box" \
    --fps 30 \
"""

import os as _os
import sys

# lerobot.policies.__init__ 우회 — 다른 lerobot import 보다 먼저여야 한다
# (groot 블록은 원래 없었다 → load_groot_config() 호출하지 않는다)
import lerobot_bootstrap  # noqa: F401

import argparse
import json
import logging
import pickle  # nosec
import signal
import threading
import time
from queue import Queue
from typing import Any

import numpy as np
from piper_bus.client import Bus

# root handler 정리 (중복 방지)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.root.handlers = [_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# ── 실시간 제어 (버스) ──
_current_task = "do the task"
_paused = False
_manual_action: dict | None = None
_target_fps: float = 20.0
_max_velocity: float = 180.0  # deg/s — 관절 최대 속도 제한 (0=무제한)
_max_gripper_velocity: float = 300.0  # %/s — 그리퍼 최대 속도 제한 (0=무제한, 범위 0~100)
_lowpass_alpha: float = 0.5  # 저역 통과 필터 계수 (1.0=필터 없음, 0.1=매우 강한 필터)
_max_jerk: float = 0.0  # deg/s² — Jerk 제한 (0=무제한)
_interpolation_steps: int = 0  # 액션 보간 스텝 수 (0=보간 없음)
_use_chunk_size: int = 0  # 수신 chunk에서 사용할 액션 수 (0=전부 사용)
_gripper_bypass_filter: bool = True  # 그리퍼 속도 제한/필터 미적용 (True=우회, 기본값)
_reset_requested: bool = False  # 원위치+리셋 요청 (실제 처리는 제어 루프)


def apply_param(key: str, value) -> bool:
    """파라미터 하나를 적용. 처리했으면 True.

    버스 실시간 변경과 **시작 시 `--config-overrides`** 가 같은 코드를 탄다.
    두 벌로 나뉘면 반드시 어긋난다 (실제로 시작 경로가 아예 없어서
    UI 슬라이더 값이 전부 유실되고 있었다).
    """
    global _current_task, _paused, _manual_action, _target_fps, _max_velocity, _max_gripper_velocity, _lowpass_alpha, _max_jerk, _interpolation_steps, _use_chunk_size, _gripper_bypass_filter, _reset_requested
    if key == "task":
        _current_task = str(value)
        logger.info("Updated task = %s", _current_task)
    elif key == "pause":
        _paused = bool(value)
        logger.info("Inference %s", "PAUSED" if _paused else "RESUMED")
    elif key == "manual_action":
        _manual_action = value if isinstance(value, dict) else None
    elif key == "fps":
        _target_fps = max(1.0, min(60.0, float(value)))
        logger.info("Updated FPS = %.1f", _target_fps)
    elif key == "max_velocity":
        _max_velocity = max(0.0, min(500.0, float(value)))
        logger.info("Updated max_velocity = %.1f deg/s", _max_velocity)
    elif key == "max_gripper_velocity":
        _max_gripper_velocity = max(0.0, min(500.0, float(value)))
        logger.info("Updated max_gripper_velocity = %.1f %%/s", _max_gripper_velocity)
    elif key == "lowpass_alpha":
        _lowpass_alpha = max(0.05, min(1.0, float(value)))
        logger.info("Updated lowpass_alpha = %.2f", _lowpass_alpha)
    elif key == "max_jerk":
        _max_jerk = max(0.0, min(5000.0, float(value)))
        logger.info("Updated max_jerk = %.1f", _max_jerk)
    elif key == "interpolation_steps":
        _interpolation_steps = max(0, min(10, int(value)))
        logger.info("Updated interpolation_steps = %d", _interpolation_steps)
    elif key == "use_chunk_size":
        _use_chunk_size = max(0, min(200, int(value)))
        logger.info("Updated use_chunk_size = %d", _use_chunk_size)
    elif key == "gripper_bypass_filter":
        _gripper_bypass_filter = bool(value)
        logger.info("Updated gripper_bypass_filter = %s", _gripper_bypass_filter)
    elif key == "reset":
        _reset_requested = True
        logger.info("Reset requested (home + clear buffers)")
    else:
        return False
    return True


def param_listener(bus: Bus) -> None:
    """버스 큐에서 파라미터를 받아 반영 (refactor/daemon-split.md 3단계).

    ZMQ PULL bind 를 대체한다. 큐라서 정책 서버 연결이 늦어도 값이 유실되지 않는다.
    """
    logger.info("파라미터 리스너 시작 (Redis 큐)")
    while True:
        try:
            msg = bus.pop_params()
            if msg is None:      # 타임아웃 — 빈 큐일 뿐이다
                continue
            for key, value in msg.items():
                apply_param(key, value)
        except Exception as e:
            logger.error("파라미터 수신 오류: %s", e)


def _save_preview(obs: dict) -> None:
    try:
        import cv2
        import tempfile
        for cam_name, cam_img in obs.items():
            if isinstance(cam_img, np.ndarray) and cam_img.ndim >= 2:
                preview_path = f"/dev/shm/piper_cam_{cam_name}.jpg"
                fd, tmp_path = tempfile.mkstemp(suffix=".jpg", dir="/dev/shm")
                try:
                    cv2.imwrite(tmp_path, cv2.cvtColor(cam_img, cv2.COLOR_RGB2BGR))
                    _os.replace(tmp_path, preview_path)
                except Exception:
                    pass
                finally:
                    try:
                        _os.close(fd)
                    except OSError:
                        pass
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="gRPC inference wrapper (lerobot 0.5+)")
    parser.add_argument("--server-address", required=True)
    parser.add_argument("--robot-type", required=True)
    parser.add_argument("--robot-port", default="")
    parser.add_argument("--robot-ports", default="",
                        help="양팔 모드: 콤마 구분 포트 (예: can_follower1,can_follower2)")
    parser.add_argument("--cameras", default="{}")
    parser.add_argument("--pretrained-path", required=True)
    parser.add_argument("--policy-type", default="smolvla")
    parser.add_argument("--policy-device", default="cuda")
    parser.add_argument("--actions-per-chunk", type=int, default=50)
    parser.add_argument("--chunk-size-threshold", type=float, default=0.9)
    parser.add_argument("--aggregate-fn", default="weighted_average",
                        choices=["weighted_average", "latest_only", "average", "conservative"])
    parser.add_argument("--offset-correction", action="store_true",
                        help="이전 chunk 마지막 액션과 새 chunk 첫 액션 사이 오프셋 제거")
    parser.add_argument("--smoothing", default="none",
                        choices=["none", "moving_avg", "exponential"],
                        help="액션 chunk 스무딩 필터 (진동 감소)")
    parser.add_argument("--smoothing-window", type=int, default=5,
                        help="이동평균 윈도우 크기")
    parser.add_argument("--task", default="do the task")
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--config-overrides", default="{}",
                        help="UI 슬라이더 시작값 JSON (필터·청크·fps). 실시간 변경과 같은 경로로 적용된다")
    parser.add_argument("--debug", action="store_true", help="주고받은 모든 데이터를 별도 폴더에 기록")
    args = parser.parse_args()

    cameras_cfg = json.loads(args.cameras)

    global _current_task
    _current_task = args.task

    # UI 슬라이더 시작값 적용 (실시간 변경과 동일 경로).
    # fps 는 --fps CLI 인자로 따로 오고 제어 루프 진입 시 _target_fps 에 반영된다.
    # 정책 파라미터(RTC/ACT)는 gRPC 모드에서 서버 쪽 소관이라 여기선 무시된다.
    for _k, _v in json.loads(args.config_overrides).items():
        if not apply_param(_k, _v):
            logger.debug("config-override 무시됨 (이 모드에서 미사용): %s", _k)

    # ── LeRobot 0.5 import ──
    import grpc
    import torch
    from lerobot.cameras.opencv.configuration_opencv import OpenCVCameraConfig  # noqa: F401
    try:
        from lerobot.cameras.realsense.configuration_realsense import RealSenseCameraConfig  # noqa: F401
    except ImportError:
        pass
    from lerobot.robots import RobotConfig, make_robot_from_config
    from lerobot.utils.import_utils import register_third_party_plugins
    from lerobot.transport import services_pb2, services_pb2_grpc
    from lerobot.transport.utils import grpc_channel_options, send_bytes_in_chunks

    # async_inference helpers (policies __init__ 우회 후 import 가능)
    from lerobot.async_inference.helpers import (
        FPSTracker,
        RemotePolicyConfig,
        TimedAction,
        TimedObservation,
        map_robot_keys_to_lerobot_features,
    )
    try:
        from lerobot.async_inference.filter import action_filter
    except ImportError:
        action_filter = lambda x: x  # lerobot 0.5: filter 없음, pass-through

    register_third_party_plugins()

    # ── 1. 로봇 생성 ──
    from concurrent.futures import ThreadPoolExecutor

    bimanual_ports = [p.strip() for p in args.robot_ports.split(",") if p.strip()] if args.robot_ports else []
    is_bimanual = len(bimanual_ports) >= 2

    def _make_robot(port, cam_cfg=None):
        RobotCfgClass = RobotConfig.get_choice_class(args.robot_type)
        cfg = RobotCfgClass(port=port)
        if cam_cfg and hasattr(cfg, "cameras"):
            from lerobot.cameras.configs import CameraConfig
            for cam_name, cam_params in cam_cfg.items():
                cp = dict(cam_params)
                cam_type = cp.pop("type", "opencv")
                CamCfgClass = CameraConfig.get_choice_class(cam_type)
                cfg.cameras[cam_name] = CamCfgClass(**cp)
        r = make_robot_from_config(cfg)
        r.connect()
        return r

    if is_bimanual:
        # 양팔 모드: left/right 로봇 + 카메라 분리
        left_cams = {k.removeprefix("left_"): v for k, v in cameras_cfg.items() if k.startswith("left_")}
        right_cams = {k.removeprefix("right_"): v for k, v in cameras_cfg.items() if k.startswith("right_")}
        shared_cams = {k: v for k, v in cameras_cfg.items() if not k.startswith("left_") and not k.startswith("right_")}
        # shared 카메라는 left에 할당
        left_cams.update(shared_cams)

        logger.info("Bimanual mode: left=%s, right=%s", bimanual_ports[0], bimanual_ports[1])
        robots = {}
        robots["left"] = _make_robot(bimanual_ports[0], left_cams)
        robots["right"] = _make_robot(bimanual_ports[1], right_cams)

        # motor_names: left_joint1.pos, ..., left_gripper.pos, right_joint1.pos, ...
        left_motors = list(robots["left"].action_features.keys())
        right_motors = list(robots["right"].action_features.keys())
        motor_names = [f"left_{m}" for m in left_motors] + [f"right_{m}" for m in right_motors]
        logger.info("Bimanual motors (%d): %s", len(motor_names), motor_names)

        # lerobot_features: left 기준 (서버에 보내는 feature map)
        lerobot_features = map_robot_keys_to_lerobot_features(robots["left"])
        robot = None  # 단일 로봇 변수는 None
    else:
        # 단일 팔 모드
        robot = _make_robot(args.robot_port, cameras_cfg)
        robots = None
        logger.info("Robot connected: %s (%d motors)", robot.name, len(robot.action_features))
        lerobot_features = map_robot_keys_to_lerobot_features(robot)
        motor_names = list(robot.action_features.keys())

    # ── 양팔 헬퍼 함수 ──
    def _get_observation():
        """단일/양팔 공통 관측 읽기."""
        if is_bimanual:
            obs = {}
            for side, r in robots.items():
                for k, v in r.get_observation().items():
                    obs[f"{side}_{k}"] = v
            return obs
        return robot.get_observation()

    def _send_action(action_dict):
        """단일/양팔 공통 액션 전송."""
        if is_bimanual:
            left_a = {k.removeprefix("left_"): v for k, v in action_dict.items() if k.startswith("left_")}
            right_a = {k.removeprefix("right_"): v for k, v in action_dict.items() if k.startswith("right_")}
            with ThreadPoolExecutor(2) as ex:
                ex.submit(robots["left"].send_action, left_a)
                ex.submit(robots["right"].send_action, right_a)
        else:
            robot.send_action(action_dict)

    def _parking_and_disconnect():
        """단일/양팔 공통 파킹 + 종료."""
        targets = robots.values() if is_bimanual else [robot]
        for r in targets:
            try:
                r.parking()
            except Exception as e:
                logger.warning("Parking failed: %s", e)
        time.sleep(5)
        for r in targets:
            try:
                r.disconnect(disable_torque=True)
            except Exception:
                pass

    # ── 2. gRPC 서버 연결 ──
    logger.info("Connecting to policy server: %s", args.server_address)
    environment_dt = 1.0 / args.fps

    policy_config = RemotePolicyConfig(
        args.policy_type, args.pretrained_path, lerobot_features,
        args.actions_per_chunk, args.policy_device,
    )
    channel = grpc.insecure_channel(
        args.server_address, grpc_channel_options(initial_backoff=f"{environment_dt:.4f}s")
    )
    stub = services_pb2_grpc.AsyncInferenceStub(channel)

    stub.Ready(services_pb2.Empty())
    stub.SendPolicyInstructions(services_pb2.PolicySetup(data=pickle.dumps(policy_config)))
    logger.info("Policy server connected")

    # ── 3. 파라미터 리스너 시작 ──
    # 주소는 `PIPER_REDIS_URL` 에서 온다 — ZMQ 시절 `--zmq-addr` 가 없어졌다.
    threading.Thread(target=param_listener, args=(Bus(),), daemon=True).start()

    # ── 4. 액션 큐 + aggregate 함수 + 오프셋 보정 + 수신 스레드 ──
    import torch

    AGGREGATE_FUNCTIONS = {
        "weighted_average": lambda old, new: 0.3 * old + 0.7 * new,
        "latest_only": lambda old, new: new,
        "average": lambda old, new: 0.5 * old + 0.5 * new,
        "conservative": lambda old, new: 0.7 * old + 0.3 * new,
    }
    aggregate_fn = AGGREGATE_FUNCTIONS[args.aggregate_fn]
    use_offset_correction = args.offset_correction
    smoothing_mode = args.smoothing
    smoothing_window = args.smoothing_window
    logger.info("Aggregate fn: %s, Offset correction: %s, Smoothing: %s (window=%d)",
                args.aggregate_fn, use_offset_correction, smoothing_mode, smoothing_window)

    action_queue = Queue()
    action_queue_lock = threading.Lock()
    latest_action_lock = threading.Lock()
    latest_action = -1
    last_executed_action = None  # 마지막으로 실행된 액션 텐서 (오프셋 보정용)
    action_chunk_size = -1
    shutdown_event = threading.Event()
    must_go = threading.Event()
    must_go.set()
    start_barrier = threading.Barrier(2)
    fps_tracker = FPSTracker(target_fps=args.fps)
    _obs_sending = threading.Event()  # obs 전송 중 플래그 (중복 전송 방지)

    # ── 디버그 레코더 (--debug 시 주고받은 모든 데이터 기록) ──
    recorder = None
    _recv_seq = 0
    _obs_seq = 0
    if args.debug:
        try:
            from debug_recorder import DebugRecorder
            recorder = DebugRecorder("server", {
                "server_address": args.server_address,
                "pretrained_path": args.pretrained_path,
                "policy_type": args.policy_type,
                "robot_type": args.robot_type,
                "fps": args.fps,
                "motor_names": motor_names,
                "cameras": list(cameras_cfg.keys()),
            })
        except Exception as e:
            logger.error("Debug recorder init failed: %s", e)

    def _actions_to_numpy(actions):
        """TimedAction 리스트를 numpy 배열로 일괄 변환 (torch→numpy 1회)."""
        return np.stack([a.get_action().numpy() for a in actions])

    def _numpy_to_actions(arr, actions_template):
        """numpy 배열을 TimedAction 리스트로 일괄 변환."""
        arr32 = arr.astype(np.float32) if arr.dtype != np.float32 else arr
        return [
            TimedAction(timestamp=a.get_timestamp(), timestep=a.get_timestep(),
                        action=torch.from_numpy(arr32[i]))
            for i, a in enumerate(actions_template)
        ]

    def _offset_correct_np(arr):
        """numpy 배열에 오프셋 보정 적용 (in-place). last_executed_action 기준."""
        if last_executed_action is None:
            return arr
        last_np = last_executed_action.numpy()
        offset = arr[0] - last_np
        blend = np.linspace(1.0, 0.0, len(arr)).reshape(-1, 1)
        arr -= offset * blend
        return arr

    def _smooth_np(arr):
        """numpy 배열에 스무딩 적용 (in-place)."""
        if smoothing_mode == "none" or len(arr) < 3:
            return arr
        n = len(arr)
        w = min(smoothing_window, n)
        first, last = arr[0].copy(), arr[-1].copy()

        if smoothing_mode == "moving_avg":
            # 벡터화된 이동평균 (전체 차원 한번에)
            cumsum = np.vstack([np.zeros((1, arr.shape[1])), np.cumsum(arr, axis=0)])
            for i in range(n):
                lo = max(0, i - w // 2)
                hi = min(n, i + w // 2 + 1)
                arr[i] = (cumsum[hi] - cumsum[lo]) / (hi - lo)
        elif smoothing_mode == "exponential":
            alpha = 2.0 / (w + 1)
            for i in range(1, n):
                arr[i] = alpha * arr[i] + (1 - alpha) * arr[i - 1]

        arr[0] = first
        arr[-1] = last
        return arr

    def _aggregate_action_queues(incoming_actions):
        """같은 타임스텝의 기존 액션과 새 액션을 aggregate_fn으로 합침."""
        nonlocal action_queue
        _t0 = time.perf_counter()

        # numpy 일괄 변환 (torch→numpy 1회만)
        arr = _actions_to_numpy(incoming_actions)
        _t1 = time.perf_counter()

        # 스무딩 + 오프셋 보정 (numpy 상태에서 처리)
        if smoothing_mode != "none":
            arr = _smooth_np(arr)
        if use_offset_correction:
            arr = _offset_correct_np(arr)
        # numpy → TimedAction 복원 (1회만)
        incoming_actions = _numpy_to_actions(arr, incoming_actions)
        _t2 = time.perf_counter()

        with action_queue_lock:
            current = {a.get_timestep(): a.get_action() for a in action_queue.queue}
        _t3 = time.perf_counter()

        with latest_action_lock:
            la = latest_action

        new_items = []
        for new_action in incoming_actions:
            ts = new_action.get_timestep()
            if ts <= la:
                continue
            if ts not in current:
                new_items.append(new_action)
            else:
                new_items.append(TimedAction(
                    timestamp=new_action.get_timestamp(),
                    timestep=ts,
                    action=aggregate_fn(current[ts], new_action.get_action()),
                ))
        _t4 = time.perf_counter()

        with action_queue_lock:
            action_queue.queue.clear()
            for item in new_items:
                action_queue.put(item)
        _t5 = time.perf_counter()

        _dlog(f"AGG_DETAIL np_conv+filter={(_t2-_t0)*1000:.1f}ms snap={(_t3-_t2)*1000:.1f}ms merge={(_t4-_t3)*1000:.1f}ms swap={(_t5-_t4)*1000:.1f}ms")

    def receive_actions():
        nonlocal action_chunk_size, _recv_seq
        start_barrier.wait()
        logger.info("Action receiver thread starting")
        while not shutdown_event.is_set():
            try:
                t_get_start = time.perf_counter()
                actions_chunk = stub.GetActions(services_pb2.Empty())
                t_get_end = time.perf_counter()
                if len(actions_chunk.data) == 0:
                    _dlog(f"RECV empty dt={(t_get_end-t_get_start)*1000:.1f}ms")
                    continue
                timed_actions = pickle.loads(actions_chunk.data)  # nosec
                # 사용할 chunk 크기 제한 (0=전부)
                if _use_chunk_size > 0 and len(timed_actions) > _use_chunk_size:
                    timed_actions = timed_actions[:_use_chunk_size]
                action_chunk_size = max(action_chunk_size, len(timed_actions))
                if recorder is not None:
                    try:
                        recorder.record_inference(_recv_seq, _actions_to_numpy(timed_actions))
                        _recv_seq += 1
                    except Exception:
                        pass
                t_agg_start = time.perf_counter()
                _aggregate_action_queues(timed_actions)
                t_agg_end = time.perf_counter()

                with action_queue_lock:
                    new_qsize = action_queue.qsize()
                _dlog(f"RECV chunk={len(timed_actions)} get={(t_get_end-t_get_start)*1000:.1f}ms agg={(t_agg_end-t_agg_start)*1000:.1f}ms qsize={new_qsize}")

                must_go.set()
            except grpc.RpcError as e:
                logger.error("Action receive error: %s", e)
                time.sleep(0.1)

    action_thread = threading.Thread(target=receive_actions, daemon=True)
    action_thread.start()

    # ── 5. 제어 루프 ──
    running = True

    def signal_handler(sig, _):
        nonlocal running
        logger.info("Signal %s received, stopping...", sig)
        running = False

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # ⚠ `_paused` 를 빠뜨리면 **제어 루프 첫 줄에서 죽는다.** 아래에서 대입하므로
    # 선언이 없으면 파이썬이 지역 변수로 보고, 읽는 순간 `UnboundLocalError` 다.
    # gRPC 원격 추론이 이것 때문에 **한 번도 돈 적이 없었다** (Total steps: 0).
    global _target_fps, _reset_requested, _paused
    _target_fps = float(args.fps)

    step = 0
    obs = {}
    _prev_loop_start: float = 0.0  # FPS 계산용: 이전 루프 시작 시간
    _actual_fps: float = 0.0
    _prev_sent: dict[str, float] = {}  # 저역 통과 필터: 이전 전송값
    _prev_velocity: dict[str, float] = {}  # Jerk 제한: 이전 속도
    _interp_from: dict[str, float] | None = None  # 보간: 시작점
    _interp_to: dict[str, float] | None = None  # 보간: 목표점
    _interp_progress: int = 0  # 보간: 현재 진행

    start_barrier.wait()
    logger.info("Control loop starting (fps=%d)", args.fps)

    # ── CSV 데이터 로그 ──
    import csv
    import datetime
    _log_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    _csv_path = f"/tmp/piper_inference_{_log_ts}.csv"
    _csv_file = open(_csv_path, "w", newline="")
    _csv_cols = (
        ["timestamp", "step", "fps", "queue_size"]
        + [f"target_{m}" for m in motor_names]
        + [f"filtered_{m}" for m in motor_names]
        + [f"actual_{m}" for m in motor_names]
        + ["task", "paused"]
    )
    _csv_writer = csv.DictWriter(_csv_file, fieldnames=_csv_cols)
    _csv_writer.writeheader()
    logger.info("CSV log: %s", _csv_path)

    # 디버그 로그 파일
    _debug_log = open("/tmp/grpc_wrapper_debug.log", "w")
    def _dlog(msg):
        _debug_log.write(f"{time.perf_counter():.4f} {msg}\n")
        _debug_log.flush()

    try:
        while running and not shutdown_event.is_set():
            loop_start = time.perf_counter()

            # 원위치+리셋: 원점 복귀 + 액션 큐/필터 상태 초기화 후 새로 시작.
            # (pause 상태와 무관하게 최상단에서 처리하고, 리셋 후 자동 재개)
            if _reset_requested:
                _reset_requested = False
                logger.info("Reset: homing robot + clearing action buffers")
                with action_queue_lock:
                    action_queue.queue.clear()
                latest_action = -1
                last_executed_action = None
                # 필터 히스토리 초기화
                _prev_sent = {}
                _prev_velocity = {}
                _interp_from = None
                _interp_to = None
                _interp_progress = 0
                # UI에 리셋(원점 복귀) 진행 중 표시
                print(json.dumps({
                    "t": "telemetry", "step": step, "fps": 0, "inference_ms": 0,
                    "joints": [], "action": [], "task": _current_task,
                    "paused": False, "resetting": True,
                }), flush=True)
                # 원점 복귀 (단계별 파킹, 블로킹 ~수초)
                for r in (robots.values() if is_bimanual else [robot]):
                    try:
                        r.parking()
                    except Exception as e:
                        logger.warning("Reset homing failed: %s", e)
                _paused = False
                continue

            # 일시정지
            if _paused:
                if _manual_action:
                    _send_action(_manual_action)
                obs = _get_observation()
                state_vals = [float(v) for k, v in obs.items() if isinstance(v, (int, float))]
                telemetry = {"t": "telemetry", "step": step, "fps": 0, "inference_ms": 0,
                             "joints": [round(v, 2) for v in state_vals], "action": [],
                             "task": _current_task, "paused": True}
                print(json.dumps(telemetry), flush=True)
                time.sleep(0.05)
                continue

            # FPS 계산 (이전 루프 시작 ~ 현재 루프 시작)
            if _prev_loop_start > 0:
                loop_dt = loop_start - _prev_loop_start
                if loop_dt > 0:
                    _actual_fps = 1.0 / loop_dt
            _prev_loop_start = loop_start

            _dlog(f"step={step} START fps={_actual_fps:.1f}")

            # (1) 액션 실행 — 보간 중이면 큐에서 꺼내지 않음
            action_dict = None
            t1 = time.perf_counter()
            interpolating = (_interpolation_steps > 0
                             and _interp_to is not None
                             and _interp_progress < _interpolation_steps)

            if interpolating:
                # 보간 진행 중: 큐에서 꺼내지 않고 보간 목표 사용
                action_dict = dict(_interp_to)
                with action_queue_lock:
                    qsize = action_queue.qsize()
            else:
                with action_queue_lock:
                    qsize = action_queue.qsize()
                    has_action = not action_queue.empty()
                if has_action:
                    with action_queue_lock:
                        timed_action = action_queue.get_nowait()
                    action_dict = {key: timed_action.get_action()[i].item() for i, key in enumerate(motor_names)}
                    last_executed_action = timed_action.get_action()
                    with latest_action_lock:
                        latest_action = timed_action.get_timestep()
                elif last_executed_action is not None:
                    action_dict = {key: last_executed_action[i].item() for i, key in enumerate(motor_names)}

            t2 = time.perf_counter()
            _dlog(f"step={step} ACTION_POP dt={(t2-t1)*1000:.1f}ms qsize={qsize} has={has_action}")

            # target 액션 저장 (필터 전 원본)
            target_action = dict(action_dict) if action_dict else None

            # 속도 제한: FPS 기반 최대 이동량 클램핑 (관절/그리퍼 분리)
            if action_dict is not None and obs:
                joint_max_delta = _max_velocity / _target_fps if _max_velocity > 0 else 0
                grip_max_delta = _max_gripper_velocity / _target_fps if _max_gripper_velocity > 0 else 0
                for key in action_dict:
                    cur = obs.get(key)
                    if cur is None or not isinstance(cur, (int, float)):
                        continue
                    is_gripper = "gripper" in key
                    max_delta = grip_max_delta if is_gripper else joint_max_delta
                    if max_delta <= 0:
                        continue
                    delta = action_dict[key] - float(cur)
                    if abs(delta) > max_delta:
                        action_dict[key] = float(cur) + max_delta * (1.0 if delta > 0 else -1.0)

            # 액션 보간: N스텝에 걸쳐 목표까지 smoothstep 보간
            if action_dict is not None and _interpolation_steps > 0:
                if not interpolating:
                    # 새 액션 도착 → 보간 시작
                    _interp_from = dict(_prev_sent) if _prev_sent else dict(action_dict)
                    _interp_to = dict(action_dict)
                    _interp_progress = 0

                if _interp_from and _interp_to and _interp_progress < _interpolation_steps:
                    t = (_interp_progress + 1) / _interpolation_steps
                    t_smooth = t * t * (3 - 2 * t)  # smoothstep
                    action_dict = {
                        k: _interp_from.get(k, v) + (_interp_to.get(k, v) - _interp_from.get(k, v)) * t_smooth
                        for k, v in action_dict.items()
                    }
                    _interp_progress += 1

            # Jerk 제한: 가속도 변화율 제한 (급방향전환 억제)
            if action_dict is not None and _max_jerk > 0 and _prev_sent:
                dt = 1.0 / _target_fps
                for key in action_dict:
                    if key not in _prev_sent:
                        continue
                    velocity = (action_dict[key] - _prev_sent[key]) / dt
                    prev_vel = _prev_velocity.get(key, velocity)
                    accel = (velocity - prev_vel) / dt
                    if abs(accel) > _max_jerk:
                        clamped_accel = _max_jerk * (1.0 if accel > 0 else -1.0)
                        velocity = prev_vel + clamped_accel * dt
                        action_dict[key] = _prev_sent[key] + velocity * dt
                    _prev_velocity[key] = velocity

            # 저역 통과 필터: 이전 전송값과 블렌딩 (고주파 떨림 제거)
            if action_dict is not None and _lowpass_alpha < 1.0 and _prev_sent:
                alpha = _lowpass_alpha
                action_dict = {
                    k: alpha * v + (1 - alpha) * _prev_sent.get(k, v)
                    for k, v in action_dict.items()
                }

            # 그리퍼 우회: 위 필터/속도제한 결과를 무시하고 원본 액션 그대로 복원
            if action_dict is not None and _gripper_bypass_filter and target_action:
                for key in action_dict:
                    if "gripper" in key and key in target_action:
                        action_dict[key] = target_action[key]

            if action_dict is not None:
                _prev_sent = dict(action_dict)
                _send_action(action_dict)
            t3 = time.perf_counter()
            _dlog(f"step={step} SEND_ACTION dt={(t3-t2)*1000:.1f}ms")

            # CSV 로그 기록 (매 스텝)
            actual_vals = {k: float(v) for k, v in obs.items() if isinstance(v, (int, float))} if obs else {}
            row = {"timestamp": f"{time.time():.4f}", "step": step,
                   "fps": round(_actual_fps, 1),
                   "queue_size": qsize, "task": _current_task, "paused": _paused}
            for m in motor_names:
                row[f"target_{m}"] = round(target_action.get(m, 0), 4) if target_action else ""
                row[f"filtered_{m}"] = round(action_dict.get(m, 0), 4) if action_dict else ""
                row[f"actual_{m}"] = round(actual_vals.get(m, 0), 4) if actual_vals else ""
            _csv_writer.writerow(row)
            if step % 30 == 0:
                _csv_file.flush()

            if recorder is not None:
                recorder.record_step(step, target_action, action_dict, actual_vals,
                                     _current_task, _paused, round(_actual_fps, 1))

            # (2) 관측 읽기 + 전송 — 큐 50% 소진 시 전송 (서버 과부하 방지)
            obs_sending_now = _obs_sending.is_set()
            with action_queue_lock:
                ready_to_send = action_chunk_size <= 0 or (action_queue.qsize() / max(action_chunk_size, 1)) <= 0.5

            _dlog(f"step={step} OBS_CHECK ready={ready_to_send} sending={obs_sending_now}")

            if ready_to_send and not obs_sending_now:
                def _read_and_send_obs():
                    nonlocal obs, _obs_seq
                    _obs_sending.set()
                    try:
                        t_obs_start = time.perf_counter()
                        obs = _get_observation()
                        t_obs_read = time.perf_counter()
                        obs["task"] = _current_task

                        with latest_action_lock:
                            la = latest_action

                        timed_obs = TimedObservation(
                            timestamp=time.time(), observation=obs, timestep=max(la, 0),
                        )
                        with action_queue_lock:
                            q_low = action_queue.qsize() <= max(action_chunk_size * 0.3, 1)
                            timed_obs.must_go = must_go.is_set() and q_low
                        if timed_obs.must_go:
                            must_go.clear()

                        obs_bytes = pickle.dumps(timed_obs)
                        t_pickle = time.perf_counter()
                        obs_iter = send_bytes_in_chunks(obs_bytes, services_pb2.Observation, log_prefix="[WEB]", silent=True)
                        stub.SendObservations(obs_iter)
                        t_send = time.perf_counter()
                        if recorder is not None:
                            recorder.record_observation(_obs_seq, obs, _current_task)
                            _obs_seq += 1
                        _dlog(f"OBS_THREAD read={(t_obs_read-t_obs_start)*1000:.1f}ms pickle={(t_pickle-t_obs_read)*1000:.1f}ms send={(t_send-t_pickle)*1000:.1f}ms total={(t_send-t_obs_start)*1000:.1f}ms")
                    except Exception as e:
                        _dlog(f"OBS_THREAD ERROR: {e}")
                        logger.error("Obs read/send error: %s", e)
                    finally:
                        _obs_sending.clear()

                threading.Thread(target=_read_and_send_obs, daemon=True).start()

            t4 = time.perf_counter()

            step += 1
            elapsed = t4 - loop_start
            _dlog(f"step={step} END dt={(elapsed)*1000:.1f}ms")

            # 텔레메트리
            if step % 5 == 0:
                state_vals = [float(v) for k, v in obs.items() if isinstance(v, (int, float))]
                fps_val = round(_actual_fps, 1)
                telemetry = {
                    "t": "telemetry", "step": step,
                    "fps": fps_val,
                    "inference_ms": round(elapsed * 1000, 1),
                    "joints": [round(v, 2) for v in state_vals],
                    "action": [round(action_dict[k], 2) for k in motor_names] if action_dict else [],
                    "task": _current_task,
                }
                print(json.dumps(telemetry), flush=True)

            # 카메라 프리뷰 (20스텝마다)
            if step % 20 == 0:
                threading.Thread(
                    target=_save_preview,
                    args=({k: v for k, v in obs.items() if isinstance(v, np.ndarray) and v.ndim >= 2},),
                    daemon=True,
                ).start()

            # FPS 제어 — 사용자 설정 FPS + 큐 잔량에 따라 동적 감속
            target_dt = 1.0 / _target_fps
            with action_queue_lock:
                q_remain = action_queue.qsize()
            if action_chunk_size > 0 and q_remain > 0:
                q_ratio = q_remain / action_chunk_size
                if q_ratio < 0.3:
                    # 30% 미만: 최대 2배 느리게 (남은 액션 늘려쓰기)
                    stretch = 1.0 + (1.0 - q_ratio / 0.3)  # 1.0~2.0
                    effective_dt = target_dt * stretch
                else:
                    effective_dt = target_dt
            else:
                effective_dt = target_dt
            sleep_time = effective_dt - (time.perf_counter() - loop_start)
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        logger.info("Interrupted")
    except Exception as e:
        logger.error("Control loop error: %s", e, exc_info=True)
    finally:
        shutdown_event.set()
        action_thread.join(timeout=3)
        logger.info("Returning to home position...")
        _parking_and_disconnect()
        logger.info("Disconnected.")
        channel.close()
        _csv_file.close()
        _debug_log.close()
        _debug_dir = recorder.close() if recorder is not None else None
        print(json.dumps({"t": "log_saved", "csv_path": _csv_path, "steps": step, "debug_dir": _debug_dir}), flush=True)
        logger.info("Done. Total steps: %d, CSV log: %s%s", step, _csv_path,
                    f", debug: {_debug_dir}" if _debug_dir else "")


if __name__ == "__main__":
    main()
