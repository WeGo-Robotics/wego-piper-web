"""추론 파라미터가 시작 시 유실되지 않는지 (refactor/01-inference-params.md).

원래 문제: 파라미터 하나가 프론트 기본값 / 슬라이더 / SAFE_PARAMS / override_keys
네 곳에 따로 적혀 있어서, 하나라도 빠지면 **에러 없이 조용히 값이 유실**됐다.
"""

import json

from app.core.cli_mapping import (
    OVERRIDE_KEYS,
    build_grpc_client_args,
    build_inference_args,
)
from app.services.param_bridge import BOOL_PARAMS, SAFE_PARAMS

# UI 슬라이더가 실제로 보내는 값들 (기본값이 아니라 "사용자가 바꿔둔" 상태)
UI_PARAMS = {
    "fps": 30,
    "max_velocity": 250,
    "max_gripper_velocity": 200,
    "lowpass_alpha": 0.3,
    "max_jerk": 1000,
    "chunk_blend_steps": 8,
    "chunk_smooth_window": 5,
    "use_chunk_size": 40,
    "refill_threshold_pct": 35,
    "gripper_bypass_filter": False,
    "max_guidance_weight": 12.0,
    "execution_horizon": 15,
    "temporal_ensemble_coeff": 0.02,
    "task": "pick the cube",
}

_LOCAL_BASE = {
    "checkpoint_path": "/m",
    "robot_type": "piper_follower",
    "robot_port": "can0",
    "device": "cuda",
    "use_amp": True,
}
_GRPC_BASE = {
    "server_address": "1.2.3.4:8088",
    "robot_type": "piper_follower",
    "robot_port": "can0",
    "checkpoint_path": "/m",
    "policy_type": "smolvla",
    "policy_device": "cuda",
    "actions_per_chunk": 100,
    "chunk_size_threshold": 0.8,
}


def _overrides_of(args: list[str]) -> dict:
    assert "--config-overrides" in args, "config-overrides 가 실리지 않았다"
    return json.loads(args[args.index("--config-overrides") + 1])


def _flag(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def test_every_ui_param_is_carried_in_local_mode():
    """UI 가 보낸 키가 하나라도 사라지면 안 된다 — 이게 원래의 유실 버그다."""
    args = build_inference_args({**_LOCAL_BASE, **UI_PARAMS})
    overrides = _overrides_of(args)
    for key, value in UI_PARAMS.items():
        if key in OVERRIDE_KEYS:
            assert overrides.get(key) == value, f"{key} 가 overrides 에서 유실"
        else:
            assert _flag(args, f"--{key.replace('_', '-')}") is not None, f"{key} 유실"


def test_every_ui_param_is_carried_in_grpc_mode():
    """gRPC 모드는 전달 경로 자체가 없어서 필터 8개 + fps 가 전부 유실됐다."""
    args = build_grpc_client_args({**_GRPC_BASE, **UI_PARAMS})
    overrides = _overrides_of(args)
    for key, value in UI_PARAMS.items():
        if key in OVERRIDE_KEYS:
            assert overrides.get(key) == value, f"{key} 가 overrides 에서 유실"


def test_both_modes_carry_the_same_overrides():
    """두 모드가 같은 집합을 실어야 한다. 갈리면 모드마다 다르게 동작한다."""
    local = _overrides_of(build_inference_args({**_LOCAL_BASE, **UI_PARAMS}))
    grpc = _overrides_of(build_grpc_client_args({**_GRPC_BASE, **UI_PARAMS}))
    assert local == grpc


def test_grpc_fps_is_not_hardcoded():
    """gRPC 분기가 fps 를 20 으로 하드코딩해 UI 슬라이더를 무시했다."""
    args = build_grpc_client_args({**_GRPC_BASE, **UI_PARAMS})
    assert _flag(args, "--fps") == "30"


def test_use_chunk_size_is_sent_at_start():
    """드리프트 (1) — override_keys 에 없어서 시작 시 버려졌다.

    슬라이더를 맞춰두고 시작하면 무시되고, 한 번 움직여 ZMQ 로 보내야 적용됐다.
    """
    assert "use_chunk_size" in OVERRIDE_KEYS
    overrides = _overrides_of(build_inference_args({**_LOCAL_BASE, **UI_PARAMS}))
    assert overrides["use_chunk_size"] == 40


def test_max_velocity_range_matches_frontend():
    """드리프트 (2) — 프론트 슬라이더 500, 백엔드 클램프 1000 으로 어긋나 있었다.

    프론트의 "관절 속도 제한(%)" 환산식이 /500 기준이므로 500 으로 통일한다.
    """
    assert SAFE_PARAMS["max_velocity"]["max"] == 500


def test_realtime_params_are_also_sent_at_start():
    """ZMQ 로 실시간 변경 가능한 값은 시작값도 전달돼야 한다.

    한쪽에만 있으면 "슬라이더를 움직여야 적용되는 값"이 생긴다 — 원래 버그의 형태다.
    `fps`/`task` 는 CLI 인자로 따로 가므로 제외한다.
    """
    realtime = (set(SAFE_PARAMS) | BOOL_PARAMS) - {"fps", "task"}
    missing = realtime - OVERRIDE_KEYS
    assert not missing, f"실시간 변경은 되는데 시작 시 전달 안 되는 파라미터: {missing}"


def test_interpolation_left_in_saved_params_goes_nowhere():
    """보간은 화면에서 뺐다 — 청크 정책에는 해롭다(inference_params.py 의 사유).

    화면만 감추면 localStorage·프리셋에 남은 값이 **보이지 않는 데서 계속 실린다.** 시작 인자에도,
    실행 중 버스에도 실리지 않아야 한다 — 그래야 wrapper 의 기본값(0, 끔)으로 돈다.
    """
    from app.services.param_bridge import ParamBridge

    stale = {"interpolation_steps": 5}
    for args in (build_inference_args({**_LOCAL_BASE, **UI_PARAMS, **stale}),
                 build_grpc_client_args({**_GRPC_BASE, **UI_PARAMS, **stale})):
        assert "interpolation_steps" not in _overrides_of(args)
        assert not any("interpolation" in a for a in args)
    safe, _ = ParamBridge(bus=None).validate_params({**stale, "lowpass_alpha": 0.6})
    assert safe == {"lowpass_alpha": 0.6}
