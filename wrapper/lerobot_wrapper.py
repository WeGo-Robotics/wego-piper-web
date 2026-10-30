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
from piper_bus.client import Bus
from policy_registry import policy_imports

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
    # temporal_ensemble(_coeff) 는 여기 없다 — policy.config 에 써 봐야 아무도 안 읽는다
    # (LeRobot 의 앙상블은 select_action 안에만 있다). wrapper 가 직접 평균한다: _ensembler
    # n_action_steps는 policy.config가 아니라 _actions_per_chunk(큐 크기)로 별도 관리 → param_listener에서 처리
    "use_amp": lambda p, v: setattr(p.config, "use_amp", bool(v)),
}

# task 텍스트 (버스로 실시간 변경 가능)
_current_task = "do the task"

# 액션 필터 (버스로 실시간 변경 가능)
from action_filter import ActionFilter
_action_filter = ActionFilter()

# 시간 앙상블 (ACT temporal ensembling — temporal_ensemble.py). 켜면 매 스텝 추론하고, 같은
# 스텝을 예측한 청크들을 가중 평균해 보낸다 — 액션 큐·재추론 트리거를 안 쓴다.
# ⚠ 꺼짐이 기본이고 켜는 스위치가 따로다. 계수(기본 0.01)는 예전부터 시작값으로 실려 왔으므로
#   계수만으로 켜면 모든 ACT 추론의 동작이 한꺼번에 바뀐다.
from temporal_ensemble import TemporalEnsembler
from delta_action import ABSOLUTE, CKPT_MARKER, DELTA_V1, DeltaRestorer
_temporal_ensemble = False
_ensembler = TemporalEnsembler()

# 청크 이어 붙이기 (chunk_merge.py) — 큐 모드에서 새 청크를 갈아끼우는 대신 경계를 크로스페이드하고
# 지연 없이 다듬는다. 시간 앙상블이 켜져 있으면 앙상블이 이미 합치므로 안 쓴다. 기본 크로스페이드 10·창 7.
from chunk_merge import ChunkMerger
_chunk_merger = ChunkMerger()

# RTC (rtc_feed.py) — flow-matching 정책(SmolVLA·π0)이 이전 청크의 남은 부분을 이어 받아 새 청크를 만든다.
# 정책이 `init_rtc_processor` 를 가질 때만 돈다. ⚠ 꺼짐이 기본 — 켜면 그 정책 추론의 청크가 바뀐다.
from rtc_feed import RtcFeed
_rtc_enabled = False
_rtc_feed = RtcFeed()

# 계획 안 순간이동 고쳐 잇기 (jump_repair.py) — 청크 안에서 말단이 한 스텝에 너무 멀리 가는 곳을 매끈하게
# 잇는다. 말단 위치(FK)가 필요해 PIPER 팔에서만 돈다 — 팔은 로봇을 연결한 뒤에 붙인다. 기본 켬.
from jump_repair import JumpRepair, piper_arms, piper_fk
_jump_repair = JumpRepair()

# 단계별 파킹 컨트롤러 (추론 종료 후 원점 복귀)
from parking_controller import ParkingController

# 일시정지 + 수동 조작
_paused = False
_manual_action: dict | None = None  # {"joint1.pos": 0.0, ...}

# 원위치+리셋: 버스 "reset" 수신 시 True → 메인 루프가 원점 복귀+버퍼/필터 초기화 수행.
# _reset_gen은 in-flight 추론 무효화용 세대 카운터 (리셋 시 증가 → 이전 세대 결과 폐기).
_reset_requested = False
_reset_gen = 0

# 다음 추론 트리거 시점: 액션 큐 잔량이 이 % 이하로 떨어지면 재추론.
#   0   = 큐가 완전히 빌 때만 (지연 동안 정지 가능)
#   100 = 매 스텝 재추론
_refill_threshold_pct = 20.0

# 추론 1회에서 받아 큐에 넣는 액션 수 (모델 chunk_size 이하로 자동 클램핑).
# 시작 시 확정하되 버스 n_action_steps로 런타임 변경 가능. 0 = 모델 전체 청크.
_actions_per_chunk = 0

# 실행 기록 — CSV 옆 .json 에 체크포인트·시작 조건·도중 변경 (run_record.py)
from run_record import RunRecord, settings
_run_record: RunRecord | None = None

# 정책 타입별 lazy import (policies/__init__.py 우회)
# 정책 → (모델모듈, 모델클래스, config모듈, config클래스).
# ⚠ **손으로 적지 않는다.** 예전에는 이 dict 와 백엔드 `POLICIES` 가 각자 목록을
# 들고 있었고, `sac` 를 학습에서 고를 수 있는데 추론에서 죽는 사고가 났다.
# 이제 둘 다 `policies/*.yaml` 한 곳에서 온다 (feature/policy-ui-spec.md).
POLICY_IMPORTS = policy_imports()


def param_listener(policy: Any, bus: "Bus") -> None:
    """버스 큐에서 파라미터를 수신하여 policy 객체에 실시간 반영.

    ZMQ PULL bind 를 Redis 큐 소비로 교체했다 (refactor/daemon-split.md 3단계).
    큐라서 pub/sub 과 달리 **정책 로딩 중에 보낸 값도 유실되지 않는다.**
    """
    global _current_task, _paused, _manual_action, _refill_threshold_pct, _actions_per_chunk, _reset_requested, _temporal_ensemble, _rtc_enabled
    logger.info("파라미터 리스너 시작 (Redis 큐)")

    while True:
        try:
            msg = bus.pop_params()
            if msg is None:      # 타임아웃 — 빈 큐일 뿐이다
                continue
            for key, value in msg.items():
                if _run_record is not None:
                    _run_record.change(key, value)
                # 일시정지/재개
                if key == "pause":
                    _paused = bool(value)
                    logger.info("Inference %s", "PAUSED" if _paused else "RESUMED")
                    continue
                # 수동 관절 조작
                if key == "manual_action":
                    _manual_action = value if isinstance(value, dict) else None
                    continue
                # 원위치+리셋 요청 (실제 처리는 메인 루프)
                if key == "reset":
                    _reset_requested = True
                    logger.info("Reset requested (home + clear buffers)")
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
                # 시간 앙상블 — 켜고 끄기의 반영(버퍼 비우기)은 메인 루프가 한다
                if key == "temporal_ensemble":
                    _temporal_ensemble = bool(value)
                    logger.info("Updated temporal_ensemble = %s", _temporal_ensemble)
                    continue
                if key == "temporal_ensemble_coeff":
                    _ensembler.set_coeff(value)
                    logger.info("Updated temporal_ensemble_coeff = %s", _ensembler.coeff)
                    continue
                if _chunk_merger.update_param(key, value):
                    logger.info("Updated chunk merge %s = %s", key, value)
                    continue
                if _jump_repair.update_param(key, value):
                    logger.info("Updated %s = %s", key, value)
                    continue
                # RTC 켜고 끄기 — 다음 추론부터 (rtc_config.enabled 는 추론 스레드가 매번 맞춘다)
                if key == "rtc_enabled":
                    _rtc_enabled = bool(value)
                    logger.info("Updated rtc_enabled = %s", _rtc_enabled)
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
            logger.error("파라미터 수신 오류: %s", e)


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
    # 양팔: "left,right" 콤마 목록 (gRPC wrapper 와 같은 규약). 있으면 bi 로봇 조립
    parser.add_argument("--robot-ports", default="")
    parser.add_argument("--cameras", default="{}")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--use-amp", action="store_true")
    parser.add_argument("--task", default=None)
    parser.add_argument("--config-overrides", default="{}")
    parser.add_argument("--debug", action="store_true", help="주고받은 모든 데이터를 별도 폴더에 기록")
    # 액션 공간 (feature/act-delta.md). 백엔드가 체크포인트의 학습 데이터셋을 보고 정한다 — 사람이 고르지 않는다
    parser.add_argument("--action-space", default="absolute", choices=["absolute", "delta_v1"])
    parser.add_argument("--delta-spec", default="{}", help='델타 채널·스텝당 상한 {"delta_names": [...], "clamp": {...}}')
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

    global _current_task, _refill_threshold_pct, _actions_per_chunk, _paused, _reset_requested, _reset_gen, _temporal_ensemble, _rtc_enabled
    _current_task = args.task if args.task else "do the task"

    # ── 액션 공간 대조 — 어긋나면 시작하지 않는다 (fail-closed) ──
    # 델타 체크포인트를 절대로 돌리면 액션 ≈ 0 → 팔이 영점 자세로 돌진하고, 절대 체크포인트를 델타로
    # 돌리면 현재 + 절대 목표 = 두 배 거리로 뛴다. 백엔드는 체크포인트를 쓸 때 표식을 남긴다 —
    # 손으로 고친 인자가 그 표식과 다르면 여기서 막는다. 표식이 없으면(옛·허브 체크포인트) 인자를 믿는다.
    # 로봇을 만들기 **전에** 본다 — 연결한 뒤에 죽으면 토크가 걸린 채 남는다.
    _marker_path = Path(args.policy_path) / CKPT_MARKER
    if _marker_path.is_file():
        _marked = json.loads(_marker_path.read_text()).get("action_space", ABSOLUTE)
        if _marked != args.action_space:
            raise SystemExit(f"액션 공간이 어긋난다: 체크포인트는 {_marked}, 인자는 {args.action_space} — "
                             f"시작하지 않는다 ({_marker_path})")
    logger.info("Action space: %s", args.action_space)

    # ── 1. 로봇 생성 ──
    # 양팔 포함 조립은 robot_factory 한 곳이다 — gRPC wrapper 와 같은 코드를 쓴다
    logger.info("Creating robot: %s (port=%s, ports=%s, cameras=%d)",
                args.robot_type, args.robot_port, args.robot_ports or "-", len(cameras_cfg))

    from robot_factory import build_robot_config

    robot_cfg, _ = build_robot_config(
        args.robot_type, args.robot_port, args.robot_ports, cameras_cfg)

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

    # RTC 를 걸 수 있는 정책이면 설정을 만들어 둔다 — 체크포인트의 `rtc_config` 는 대개 None 이라
    # 아래 오버라이드의 RTC 슬라이더가 "rtc_config is None" 으로 버려졌다. 켜기는 추론 스레드가 매번 한다.
    # 가중치를 얼린다: RTC 가이던스는 enable_grad 안에서 디노이저를 돌리는데, 가중치가 grad 를 요구하면
    # 매 스텝 네트워크 전체의 그래프를 쌓는다 (가이던스 자체는 출력에 대한 기울기만 쓴다).
    _rtc_capable = hasattr(policy, "init_rtc_processor") and hasattr(policy_cfg, "rtc_config")
    if _rtc_capable:
        from lerobot.policies.rtc.configuration_rtc import RTCConfig
        if policy_cfg.rtc_config is None:
            policy_cfg.rtc_config = RTCConfig(enabled=False)
        policy.init_rtc_processor()
        policy.requires_grad_(False)
        logger.info("RTC capable (execution_horizon=%s, max_guidance_weight=%s)",
                    policy_cfg.rtc_config.execution_horizon, policy_cfg.rtc_config.max_guidance_weight)

    # config 오버라이드 적용
    # (n_action_steps/use_chunk_size는 policy.config에 적용하지 않음 — actions_per_chunk로 별도 관리)
    #
    # 두 키가 같은 _actions_per_chunk를 가리킨다. use_chunk_size가 UI의 정식 슬라이더이고
    # 0 = "지정 안 함(모델 전체)" 이므로, 0이 아니면 use_chunk_size가 이긴다.
    # n_action_steps는 예전 localStorage 값이 남아 들어올 수 있어 폴백으로만 둔다.
    _actions_per_chunk_override = None
    for key, value in config_overrides.items():
        if key == "use_chunk_size":
            if int(value) > 0:
                _actions_per_chunk_override = int(value)
                logger.info("actions_per_chunk override: %s (use_chunk_size)", value)
            continue
        if key == "n_action_steps":
            if _actions_per_chunk_override is None:
                _actions_per_chunk_override = int(value)
                logger.info("actions_per_chunk override: %s (n_action_steps)", value)
            continue
        if key == "refill_threshold_pct":
            _refill_threshold_pct = max(0.0, min(100.0, float(value)))
            logger.info("refill_threshold_pct override: %s", _refill_threshold_pct)
            continue
        if key == "temporal_ensemble":
            _temporal_ensemble = bool(value)
            logger.info("temporal_ensemble override: %s", _temporal_ensemble)
            continue
        if key == "temporal_ensemble_coeff":
            _ensembler.set_coeff(value)
            logger.info("temporal_ensemble_coeff override: %s", _ensembler.coeff)
            continue
        if _chunk_merger.update_param(key, value):
            logger.info("Chunk merge override: %s = %s", key, value)
            continue
        if _jump_repair.update_param(key, value):
            logger.info("%s override: %s", key, value)
            continue
        if key == "rtc_enabled":
            _rtc_enabled = bool(value)
            logger.info("rtc_enabled override: %s", _rtc_enabled)
            continue
        # 액션 필터 시작값 (런타임 변경과 동일 경로로 적용)
        if _action_filter.update_param(key, value):
            logger.info("Filter override: %s = %s", key, value)
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

    # ── 실행 기록 — 리스너보다 먼저 연다: 로딩 중에 큐에 쌓인 값도 "도중 변경"에 남게 ──
    import datetime as _dt
    _log_ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    _csv_path = f"/tmp/piper_inference_{_log_ts}.csv"
    global _run_record
    _run_record = RunRecord(
        _csv_path, wrapper="local", policy_type=policy_type, checkpoint=local_path,
        args={**vars(args), "cameras": cameras_cfg, "config_overrides": config_overrides},
        model={k: getattr(policy_cfg, k, None) for k in ("chunk_size", "n_action_steps", "n_obs_steps")},
        # 시작값 = 기본값 + config_overrides
        refill_threshold_pct=_refill_threshold_pct, temporal_ensemble=_temporal_ensemble,
        temporal_ensemble_coeff=_ensembler.coeff, filter=settings(_action_filter))
    logger.info("Run record: %s", _run_record.path)

    # ── 4. 파라미터 리스너 시작 (Redis 큐) ──
    # 주소는 `PIPER_REDIS_URL` 에서 온다 — ZMQ 시절 `--zmq-addr` 가 없어졌다.
    param_thread = threading.Thread(
        target=param_listener, args=(policy, Bus()), daemon=True)
    param_thread.start()

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
    _fps_history: list[float] = []       # 스텝 처리 시간 (슬립 제외)
    _loop_history: list[float] = []      # 스텝 시작 간격 = 실제 제어 주기
    _loop_prev: list[float | None] = [None]
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

    # ── CSV 로그 (경로는 실행 기록과 같이 위에서 정했다) ──
    import csv
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
    _run_record.update(actions_per_chunk=_actions_per_chunk)

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
    # (관측을 찍은 스텝, 그때의 앙상블 세대, 관측, RTC 이어 받기). 스텝이 있어야 청크의 k 번째가 어느 스텝인지 안다.
    # 한 튜플로 넘긴다 — 따로 대입하면 추론 스레드가 섞인 조합을 읽을 수 있다. RTC 이어 받기는
    # (이전 청크의 남은 원본, inference_delay) 또는 None — 관측과 **같은 순간**의 큐에서 떠야 맞물린다.
    _obs_for_inference: tuple[int, int, dict, tuple[np.ndarray, int] | None] | None = None
    # 이번 루프가 실제로 쓰는 모드. 스위치(_temporal_ensemble)가 바뀌면 메인 루프가 버퍼를 비우며 따라간다.
    _te_active = False
    # stage 헤드 정책(ACT-S·SmolVLA-S)이 청크마다 내는 stage 예측. 다른 정책이면 계속 None 이다.
    _latest_aux: dict | None = None
    # 이름표는 체크포인트 config 에 들어 있다 — 추론이 piper_phase 를 import 하지
    # 않고도 클래스 순서를 안다. 다른 태스크로 구운 모델이면 이름도 그 모델 것이다.
    _stage_names: list[str] = list(getattr(policy.config, "stage_names", []) or [])
    _obs_event = threading.Event()
    _inference_running = True
    _inference_ms_shared = 0.0
    _infer_seq = 0
    _inference_in_flight = False  # 연산 중 재트리거 방지 (이벤트는 연산 시작 시 clear되므로 별도 플래그 필요)
    _effective_chunk_len = max(1, _actions_per_chunk)  # 모델 청크로 클램핑된 실제 청크 길이 — refill_lead 계산용
    # 액션을 꺼내 간 마지막 스텝 (락 안에서 쓴다). 청크를 이어 붙일 때 관측 뒤 몇 칸이 이미 나갔는지 센다.
    _queue_step = -1
    motor_names = list(robot.action_features.keys())
    # 그리퍼 우회가 켜져 있으면 청크 이어 붙이기도 그리퍼에 손대지 않는다 — ActionFilter 와 같은 판정
    _gripper_dims = [j for j, name in enumerate(motor_names) if "gripper" in name]
    # 고쳐 잇기는 PIPER 팔에만 붙는다. 못 붙이면 꺼진 채로 추론은 그대로 돈다 — 다듬기일 뿐 안전장치가 아니다.
    _arms = piper_arms(args.robot_type, motor_names)
    _fk = piper_fk() if _arms else None
    if _fk is not None:
        _jump_repair.attach(_fk, _arms, args.fps)
        logger.info("Jump repair: %d arm(s), limit %.1fmm/step, %s", len(_arms), _jump_repair.limit,
                    "ON" if _jump_repair.on else "OFF")
    else:
        logger.info("Jump repair unavailable (needs PIPER kinematics)")
    _run_record.update(jump_repair=_jump_repair.enabled)
    # 델타 체크포인트면 청크를 절대로 되돌린다 (delta_action.py). 채널이 로봇에 없으면 여기서 죽는다.
    _delta: DeltaRestorer | None = None
    if args.action_space == DELTA_V1:
        _spec = json.loads(args.delta_spec)
        _delta = DeltaRestorer(motor_names, list(_spec.get("delta_names") or []), dict(_spec.get("clamp") or {}))
        logger.info("Delta actions: %s (clamp %s)", _delta.names, _spec.get("clamp"))

    def _inference_thread_fn():
        """별도 스레드에서 predict_action_chunk() 실행 (서버 방식)."""
        nonlocal _latest_action_dict, _latest_action_np, _inference_ms_shared, _action_queue, _infer_seq, _inference_in_flight, _effective_chunk_len, _latest_aux
        while _inference_running:
            triggered = _obs_event.wait(timeout=1.0)
            if not _inference_running:
                break
            # 타임아웃(새 관측 없음)이면 재추론 금지 — 낡은 obs로 큐를 채워
            # 메인 루프의 큐-소진 재추론(_obs_for_inference 갱신)을 막는 것을 방지.
            if not triggered:
                continue
            _obs_event.clear()
            _inference_in_flight = True

            pending = _obs_for_inference
            if pending is None:
                _inference_in_flight = False
                continue
            obs_step, obs_epoch, raw_obs, rtc = pending

            # 리셋 세대 캡처: 연산 도중 리셋이 발생하면 이 결과는 폐기(스톨 액션 방지)
            my_gen = _reset_gen


            seq = _infer_seq
            _infer_seq += 1
            if recorder is not None:
                recorder.record_observation(seq, raw_obs, _current_task)

            t0 = time.monotonic()
            try:
                # RTC — 이어 받을 것이 있을 때만 켠다. 없으면 예전과 한 비트도 다르지 않게 끈다.
                # ⚠ inference_mode 안에서는 RTC 가 죽는다: 가이던스가 enable_grad 로 출력에 대한 기울기를
                #   구하는데 inference_mode 가 그걸 막는다 ("element 0 of tensors does not require grad",
                #   SmolVLA-S 실모델 확인). 그래서 RTC 일 때만 no_grad 다.
                rtc_kwargs = {}
                if _rtc_capable:
                    policy.config.rtc_config.enabled = rtc is not None
                    if rtc is not None:
                        rtc_prev, rtc_delay = rtc
                        rtc_kwargs = {
                            "inference_delay": rtc_delay,
                            "prev_chunk_left_over": torch.as_tensor(rtc_prev, dtype=torch.float32, device=device).unsqueeze(0),
                        }
                with (
                    torch.no_grad() if rtc_kwargs else torch.inference_mode(),
                    torch.autocast(device_type=device.type) if device.type == "cuda" and policy.config.use_amp else nullcontext(),
                ):
                    observation = _prepare_observation(raw_obs)
                    # predict_action_chunk — 서버와 동일
                    action_chunk = policy.predict_action_chunk(observation, **rtc_kwargs)
                    # ACT-S·SmolVLA-S 는 같은 인코더 위에서 stage 를 함께 낸다. 바닐라
                    # 정책엔 이 속성이 없으므로 getattr 로 막는다 — 정책 타입 분기가
                    # wrapper 로 새어 들어오지 않게 한다.
                    aux = getattr(policy, "last_aux", None)
                    if action_chunk.ndim != 3:
                        action_chunk = action_chunk.unsqueeze(0)
                    # (B, chunk_size, action_dim) → (n, action_dim).
                    # 원하는 크기가 모델 실제 청크 길이를 넘지 않도록 클램핑 (0=전체).
                    _model_chunk = action_chunk.shape[1]
                    _apc = _model_chunk if _actions_per_chunk <= 0 else max(1, min(_actions_per_chunk, _model_chunk))
                    action_chunk = action_chunk[0, :_apc, :]
                    # 정규화 원본 — 다음 추론의 RTC 가 이걸 이어 받는다 (후처리한 큐로는 못 잇는다)
                    raw_chunk = action_chunk.float().cpu().numpy()

                # postprocessor 적용 (unnormalize) + 액션 큐에 추가
                new_queue = []
                for i in range(action_chunk.shape[0]):
                    action_i = action_chunk[i].unsqueeze(0)  # (1, action_dim)
                    if postprocessor is not None:
                        action_i = postprocessor(action_i)
                    a_np = action_i.squeeze(0).cpu().numpy()
                    a_dict = {name: float(a_np[j]) for j, name in enumerate(motor_names)}
                    new_queue.append((a_np, a_dict))

                # 델타 → 절대. 앵커는 이 추론에 넣은 관측이다 — 지금 다시 읽지 않는다.
                # 앙상블·큐 어느 쪽이든 아래부터는 절대 위치다.
                if _delta is not None and new_queue:
                    restored = _delta.restore(np.stack([a for a, _ in new_queue]), _delta.anchor(raw_obs))
                    new_queue = [(r, {name: float(r[j]) for j, name in enumerate(motor_names)}) for r in restored]

                # 청크 안의 순간이동을 잇는다 (jump_repair.py) — 앙상블·병합·RTC 어느 쪽으로 가든 그 전에.
                # 병합의 크로스페이드는 경계만 잇는다. 디버그 기록은 고치기 전 청크에 고친 자리만 덧붙인다.
                recorded_queue = new_queue
                repaired: list = []
                if _jump_repair.enabled and new_queue:
                    fixed_chunk, repaired = _jump_repair.repair(np.stack([a for a, _ in new_queue]))
                    if repaired:
                        new_queue = [(r, {name: float(r[j]) for j, name in enumerate(motor_names)}) for r in fixed_chunk]
                        logger.info("Jump repaired (seq %d): %s", seq, repaired)

                with _action_lock:
                    # 연산 도중 리셋이 발생했으면(_reset_gen 변경) 이 결과는 낡은 것 → 폐기
                    if my_gen != _reset_gen:
                        logger.info("Discarding stale inference result (reset occurred)")
                        continue
                    elapsed = _queue_step + 1 - obs_step
                    if _te_active:
                        # 큐를 갈아끼우지 않는다 — 관측 스텝 기준으로 평균에 더한다. 그 사이 이미 보낸
                        # 칸은 앙상블이 버리고, 비운 뒤에 도착한 옛 관측의 청크는 세대가 달라 버린다.
                        _ensembler.add(obs_step, [a for a, _ in new_queue], epoch=obs_epoch)
                    elif _chunk_merger.enabled and new_queue:
                        # 갈아끼우면 경계에서 팔이 되돌아간다 — 남은 큐에 이어 붙인다 (chunk_merge.py).
                        # ⚠ 이후 CSV·텔레메트리의 target 은 정책 원본이 아니라 이어 붙인 값이다.
                        #   원본 청크는 디버그 기록(inference.jsonl)에 남는다.
                        merged = _chunk_merger.merge(
                            [a for a, _ in new_queue], [a for a, _ in _action_queue],
                            elapsed=elapsed, last_sent=_latest_action_np,
                            keep=_gripper_dims if _action_filter.gripper_bypass_filter else (),
                        )
                        _action_queue = [(r, {name: float(r[j]) for j, name in enumerate(motor_names)})
                                         for r in merged]
                    elif rtc_kwargs and new_queue:
                        # RTC 는 앞 칸을 이전 계획에 맞춰 둔 청크다 — 이미 지나간 칸을 버려야 맞물린다 (LeRobot 과 같다)
                        _action_queue = new_queue[min(max(0, elapsed), len(new_queue) - 1):]
                    else:
                        _action_queue = new_queue
                    if not _te_active:
                        _rtc_feed.replace(raw_chunk, len(_action_queue))
                        # 첫 청크(워밍업이라 느리다)는 지연 기록에 안 넣는다 — 넣으면 한동안 앞을 과하게 고정한다
                        if obs_step > 0:
                            _rtc_feed.note_elapsed(elapsed)
                    # 액션과 같은 추론에서 나온 값이므로 같은 세대 검사를 탄다.
                    # 위에서 폐기됐으면 stage 도 공표되지 않는다.
                    _latest_aux = aux
                    # 실제 큐에 담긴 길이 = 모델 청크로 클램핑된 값. refill_lead가 raw 슬라이더
                    # 값(클램핑 전)을 쓰면 청크보다 큰 임계치가 나와 매번 재추론하므로 이 값을 사용.
                    _effective_chunk_len = max(1, len(new_queue))
                    # 첫 액션을 latest로 설정 (앙상블이면 latest 는 실제로 보낸 평균이다)
                    if _action_queue and not _te_active:
                        _latest_action_np, _latest_action_dict = _action_queue[0]

                _inference_ms_shared = round((time.monotonic() - t0) * 1000, 1)

                if recorder is not None:
                    recorder.record_inference(
                        seq,
                        action_chunk.cpu().numpy(),
                        [a_dict for (_, a_dict) in recorded_queue],
                        _inference_ms_shared,
                        repaired=repaired,
                    )
            except Exception as e:
                logger.error("Inference thread error: %s", e, exc_info=True)
            finally:
                _inference_in_flight = False

    inference_thread = threading.Thread(target=_inference_thread_fn, daemon=True)
    inference_thread.start()

    try:
        while running:
            start_time = time.monotonic()

            # 원위치+리셋: 원점 복귀 + 버퍼/필터/policy 초기화 후 첫 추론처럼 새로 시작.
            # (pause 상태와 무관하게 최상단에서 처리하고, 리셋 후에는 자동 재개)
            if _reset_requested:
                _reset_requested = False
                logger.info("Reset: homing robot + clearing action buffers")
                # in-flight 추론 무효화 + 다음 트리거 대기 초기화
                _reset_gen += 1
                _obs_event.clear()
                with _action_lock:
                    _action_queue = []
                    _ensembler.clear(0)
                    _rtc_feed.clear()
                    _latest_action_dict = None
                    _latest_action_np = None
                    _effective_chunk_len = 1
                    _queue_step = -1
                _action_filter.reset()
                try:
                    policy.reset()
                except Exception as e:
                    logger.warning("policy.reset() failed: %s", e)
                # UI에 리셋(원점 복귀) 진행 중 표시
                print(json.dumps({
                    "t": "telemetry", "step": step, "fps": 0, "inference_ms": 0,
                    "joints": [], "action": [], "task": _current_task,
                    "paused": False, "resetting": True,
                }), flush=True)
                # 원점 복귀 (단계별 파킹, 블로킹 ~수초)
                try:
                    ParkingController(robot).run()
                except Exception as e:
                    logger.warning("Reset homing failed: %s", e)
                # 첫 추론처럼 새로 시작: 자동 재개 + step=0 → 다음 이터레이션 첫 추론 트리거
                _paused = False
                step = 0
                continue

            # 일시정지 중: 수동 액션만 처리
            if _paused:
                # 멈추기 전에 쌓인 예측은 재개 시점엔 낡았다. 앙상블은 오래된 예측을 더 믿으므로 남겨 두면
                # 재개 직후 몇 초를 낡은 궤적이 끈다 — 비운다. (큐 모드는 예전 그대로 둔다.)
                with _action_lock:
                    if _te_active and len(_ensembler):
                        _ensembler.clear(step)
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

            # 시간 앙상블 켜고 끄기 — 큐와 앙상블을 섞지 않는다. 둘 다 비우고 다음 청크부터 새로.
            # 그 사이(추론 1회)는 큐가 빈 것과 같아 직전 액션을 유지한다.
            if _temporal_ensemble != _te_active:
                with _action_lock:
                    _te_active = _temporal_ensemble
                    _action_queue = []
                    _ensembler.clear(step)
                    _rtc_feed.clear()
                logger.info("Temporal ensemble %s", "ON" if _te_active else "OFF")

            # 5a. 관측값 가져오기
            obs = robot.get_observation()

            if step == 0:
                logger.info("Observation keys: %s", list(obs.keys()))
                # 첫 추론 트리거
                _obs_for_inference = (step, _ensembler.epoch, {k: v.copy() if isinstance(v, np.ndarray) else v for k, v in obs.items()}, None)
                _obs_event.set()

            # 5b. 관절값 추출 (텔레메트리용)
            state_values = [float(v) for k, v in obs.items() if isinstance(v, (int, float))]

            # 5c. 액션 큐에서 순서대로 꺼내서 로봇에 전송
            with _action_lock:
                # 큐가 비어 직전 액션을 유지해도 이 스텝은 지나간 것이다
                _queue_step = step
                if _te_active:
                    # 이 스텝을 예측한 청크들의 가중 평균. 덮는 청크가 없으면 직전 액션 유지
                    ensembled = _ensembler.pop(step)
                    if ensembled is not None:
                        _latest_action_np = ensembled
                        _latest_action_dict = {name: float(ensembled[j]) for j, name in enumerate(motor_names)}
                    action_np, action_dict = _latest_action_np, _latest_action_dict
                    qsize_after = len(_ensembler)
                else:
                    if _action_queue:
                        action_np, action_dict = _action_queue.pop(0)
                        _rtc_feed.pop()
                        _latest_action_np = action_np
                        _latest_action_dict = action_dict
                    else:
                        action_dict = _latest_action_dict
                        action_np = _latest_action_np
                    qsize_after = len(_action_queue)

            # 큐 잔량이 임계치(%) 이하로 떨어질 때만 새 추론을 트리거 (매 스텝 X).
            # 임계치를 높이면 큐가 비기 전에 미리 재추론해 지연을 감추고(액션 일부 폐기),
            # 낮추면 청크를 더 소진한 뒤 재추론(폐기 최소화, 지연 노출 위험).
            _refill_lead = _effective_chunk_len * (_refill_threshold_pct / 100.0)
            # 이벤트 대기(set)뿐 아니라 실제 연산 중(_inference_in_flight)에도 재트리거 금지 —
            # 연산 시작 시 이벤트가 clear되므로 이 플래그가 없으면 연산 도중 큐가 임계치 이하로
            # 떨어져 중복 추론이 짝지어 발생함.
            # 시간 앙상블이면 매 스텝 — 평균할 청크가 매 스텝 있어야 한다. 추론이 제어보다 느리면
            # 연산 중 플래그에 걸려 끝나는 대로 다음 것을 돌린다.
            if (_te_active or qsize_after <= _refill_lead) and not _obs_event.is_set() and not _inference_in_flight:
                # RTC 이어 받기는 관측과 같은 순간의 큐에서 뜬다. 델타 체크포인트는 앵커가 관측마다 달라
                # 이전 청크의 원본(델타)을 새 청크 공간으로 옮길 수 없다 — RTC 를 안 건다.
                rtc = None
                if _rtc_enabled and _rtc_capable and _delta is None and not _te_active:
                    with _action_lock:
                        left = _rtc_feed.left_over()
                        if left is not None:
                            rtc = (left, _rtc_feed.delay())
                _obs_for_inference = (step, _ensembler.epoch, {k: v.copy() if isinstance(v, np.ndarray) else v for k, v in obs.items()}, rtc)
                _obs_event.set()

            filtered = None
            if action_dict is not None:
                # 현재 관절 상태 (속도 제한용)
                current_state = {k: v for k, v in obs.items() if isinstance(v, (int, float))}
                filtered = _action_filter.apply(action_dict, current_state)
                robot.send_action(filtered)

            step += 1
            elapsed = time.monotonic() - start_time
            # ⚠ **두 값은 다른 것을 잰다. 섞으면 오해한다.**
            #
            #   step_ms  — 한 스텝 **처리**에 쓴 시간(슬립 제외). 목표 주기 대비
            #              얼마나 여유가 있는지. 카메라 대기가 여기 들어간다.
            #   loop_fps — 슬립까지 포함한 **실제 제어 주기**. 팔이 실제로 이 속도로
            #              움직인다. 목표에 못 미치면 그때가 진짜 경고다.
            #
            # 예전에는 처리 시간의 역수를 `fps` 라고 불러서, 카메라를 USB 3 으로
            # 옮겨 대기가 사라지자 1000 이 떴다 — 루프는 그대로 20Hz 인데.
            _fps_history.append(elapsed)
            if len(_fps_history) > 20:
                _fps_history.pop(0)
            step_ms = round(1000 * sum(_fps_history) / len(_fps_history), 1)

            now = time.monotonic()
            if _loop_prev[0] is not None:
                _loop_history.append(now - _loop_prev[0])
                if len(_loop_history) > 20:
                    _loop_history.pop(0)
            _loop_prev[0] = now
            current_fps = round(
                len(_loop_history) / max(sum(_loop_history), 1e-6), 1
            ) if _loop_history else 0.0

            # 텔레메트리
            telemetry = {
                "t": "telemetry",
                "step": step,
                "fps": current_fps,          # 실제 제어 주기 (슬립 포함)
                "step_ms": step_ms,          # 한 스텝 처리 시간 — 목표 대비 여유
                "inference_ms": _inference_ms_shared,
                "joints": [round(v, 2) for v in state_values],
                "action": [round(float(action_np[i]), 2) for i in range(len(action_np))] if action_np is not None else [],
                "task": _current_task,
            }
            # ACT-S·SmolVLA-S 만 채워진다. 추론마다 한 번 갱신된다 — 큐 모드면 같은 값이 청크
            # 길이만큼(15fps·apc 20~50 → 1~3초) 반복되고, 시간 앙상블이면 매 스텝 바뀐다.
            with _action_lock:
                aux_now = _latest_aux
            if aux_now is not None:
                idx = aux_now.get("stage")
                if idx is not None and 0 <= idx < len(_stage_names):
                    telemetry["stage"] = _stage_names[idx]
                    telemetry["stage_p"] = round(float(aux_now.get("stage_p", 0.0)), 3)
            if step % 50 == 0 and torch.cuda.is_available():
                telemetry["gpu_mem_mb"] = round(torch.cuda.memory_allocated() / 1024 / 1024)
                telemetry["gpu_total_mb"] = round(torch.cuda.get_device_properties(0).total_memory / 1024 / 1024)
            print(json.dumps(telemetry), flush=True)

            # CSV 로그
            with _action_lock:
                qsize = len(_ensembler) if _te_active else len(_action_queue)
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
        if _delta is not None:
            logger.info("Delta clamp hit on %d steps", _delta.clipped_steps)
        logger.info("Returning to home position...")
        ctrl = ParkingController(robot)
        try:
            # 이상 시 False(모션 스킵), 정상 시 단계별 파킹 후 True
            parked = ctrl.run()
            if parked:
                time.sleep(0.5)  # 토크 차단 전 최종 정지 안정화 여유
        except Exception as e:
            logger.warning("Parking failed: %s", e)
        logger.info("Disabling torque and disconnecting...")
        try:
            # disconnect(disable_torque=True)는 내부에서 기존 동시-파킹을 재호출하므로,
            # 토크는 여기서 명시적으로 끊고(closePort 전 CAN 전송) disconnect는 파킹 없이 호출.
            ctrl.disable_torque()
            robot.disconnect(disable_torque=False)
        except Exception:
            pass
        _csv_file.close()
        _run_record.finish(step)
        _debug_dir = recorder.close() if recorder is not None else None
        print(json.dumps({"t": "log_saved", "csv_path": _csv_path, "steps": step, "debug_dir": _debug_dir}), flush=True)
        logger.info("Done. Total steps: %d, CSV log: %s%s", step, _csv_path,
                    f", debug: {_debug_dir}" if _debug_dir else "")


if __name__ == "__main__":
    main()
