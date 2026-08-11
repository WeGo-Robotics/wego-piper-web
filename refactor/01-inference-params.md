# 1. 추론 파라미터 3중 정의 (A급 — 드리프트 2건 발생 중)

## 문제

추론 파라미터 하나가 네 군데에 따로 적혀 있다.

| 위치 | 역할 |
|---|---|
| [InferencePage.tsx:108-114](../frontend/src/pages/InferencePage.tsx#L108-L114) | 기본값 |
| [InferencePage.tsx:643-707](../frontend/src/pages/InferencePage.tsx#L643-L707) | 슬라이더 min/max/step/라벨 |
| [zmq_bridge.py:16-35](../backend/app/services/zmq_bridge.py#L16-L35) | `SAFE_PARAMS` 런타임 클램프 범위 / `BOOL_PARAMS` / `UNSAFE_PARAMS` |
| [cli_mapping.py:50-56](../backend/app/core/cli_mapping.py#L50-L56) | `override_keys` — 추론 시작 시 `--config-overrides`로 실을 키 |

파라미터를 하나 추가하려면 네 곳을 모두 고쳐야 하고, 하나라도 빠지면 **조용히 값이 유실된다**
(에러 없음). 실제로 두 건이 이미 어긋나 있다.

## 드리프트 (1) — `use_chunk_size`가 시작 시 유실

`use_chunk_size`는 프론트 기본값에도, 슬라이더에도, `SAFE_PARAMS`에도 있고
양쪽 wrapper가 ZMQ로 처리한다:

- [lerobot_wrapper.py:167-169](../wrapper/lerobot_wrapper.py#L167-L169) — `_actions_per_chunk`에 반영
- [grpc_wrapper.py:119-121](../wrapper/grpc_wrapper.py#L119-L121) — `_use_chunk_size`에 반영

그런데 [cli_mapping.py](../backend/app/core/cli_mapping.py#L50-L56)의 `override_keys`에는 없다:

```python
override_keys = {
    "max_guidance_weight", "execution_horizon", "temporal_ensemble_coeff",
    "n_action_steps", "refill_threshold_pct",
    "max_velocity", "max_gripper_velocity", "lowpass_alpha",
    "max_jerk", "interpolation_steps", "gripper_bypass_filter",
}   # ← use_chunk_size 없음
```

`INFERENCE_ARGS_MAP`에도 없으므로 [build_inference_args](../backend/app/core/cli_mapping.py#L58-L63)의
`cli_flag is None` 분기에서 그냥 버려진다.

**증상**: UI에서 "받는 액션 청크 크기"를 맞춰두고 추론을 시작하면 그 값이 무시된다.
슬라이더를 한 번 움직여 ZMQ로 다시 보내야 적용된다.

커밋 `3e9e399`(Apply action-filter values at inference start)에서 필터값들을 `override_keys`에
넣을 때 이것만 누락된 것으로 보인다.

## 드리프트 (2) — `max_velocity` 범위 불일치

- 슬라이더: [InferencePage.tsx:645](../frontend/src/pages/InferencePage.tsx#L645) `min={0} max={500}`
- 백엔드: [zmq_bridge.py:22](../backend/app/services/zmq_bridge.py#L22) `{"min": 0, "max": 1000}`

프론트가 더 좁아서 당장 위험하진 않지만(백엔드 클램프가 발동할 일이 없음),
어느 쪽이 의도한 상한인지 불명확하다. 나머지 11개 파라미터는 범위가 일치한다.

## 함께 정리할 것 — 노브 2개가 같은 변수를 건드림

로컬 모드에서 `use_chunk_size`와 `n_action_steps` 슬라이더가 런타임에 **똑같이
`_actions_per_chunk`를 쓴다** ([lerobot_wrapper.py:167-168](../wrapper/lerobot_wrapper.py#L167-L168)):

```python
if key in ("use_chunk_size", "n_action_steps"):
    _actions_per_chunk = max(0, int(value)) if key == "use_chunk_size" else max(1, int(value))
```

시작 시엔 `n_action_steps`만 적용되고([lerobot_wrapper.py:286-290](../wrapper/lerobot_wrapper.py#L286-L290)),
gRPC 모드에서는 `use_chunk_size`가 별개 변수(`_use_chunk_size`)이고 `actions_per_chunk`는
CLI 인자다. 즉 **모드에 따라 두 슬라이더의 의미가 다르다.**

구조 통합 전에 이 둘의 의도를 먼저 정해야 한다:
- (a) 로컬/gRPC 모두에서 별개 개념으로 유지 → UI에서 모드별로 표시 분리
- (b) 로컬에서는 하나로 합침 → 슬라이더 하나 제거

## 해결안

### 단계 1 — 핀포인트 수정 (먼저 할 것)

1. `override_keys`에 `use_chunk_size` 추가 — 단, 위 "노브 2개" 문제 때문에 로컬 모드에서
   `n_action_steps`와 충돌한다. 어느 쪽이 이기는지 정한 뒤 넣어야 한다.
2. `max_velocity` 상한을 500/1000 중 하나로 통일.

### 단계 2 — 구조 통합

백엔드에 파라미터 스펙 단일 소스를 만든다:

```python
# backend/app/core/inference_params.py
PARAM_SPEC = {
    "max_velocity": {
        "label": "관절 속도 (deg/s)", "min": 0, "max": 500, "step": 10, "default": 180,
        "realtime": True,     # ZMQ로 실시간 변경 가능 (= 기존 SAFE_PARAMS)
        "send_at_start": True, # 시작 시 --config-overrides에 포함 (= 기존 override_keys)
    },
    ...
}
```

- `zmq_bridge.SAFE_PARAMS` / `BOOL_PARAMS` → `PARAM_SPEC`에서 파생
- `cli_mapping.override_keys` → `PARAM_SPEC`에서 파생
- `GET /api/params/spec`로 프론트에 제공 → 슬라이더를 스펙에서 생성

프론트가 스펙을 받아 렌더하면 기본값·범위·라벨이 어긋날 수가 없어진다.
`RTC 전용`(max_guidance_weight 등) 표시는 스펙에 `policies: ["smolvla","pi0","pi05"]`
필드를 두면 #2(정책 레지스트리)와도 맞물린다.

## 검증

- `use_chunk_size`를 0이 아닌 값으로 두고 추론 시작 → wrapper 로그에
  `actions_per_chunk: N` 이 UI 값과 일치하는지 확인
- `cd frontend && npm run build`
- 슬라이더 전 항목을 극단값으로 밀어보고 백엔드 클램프 로그 확인

## 상태

☐ 미착수
