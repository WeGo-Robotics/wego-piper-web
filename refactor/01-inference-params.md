# 1. 추론 파라미터 3중 정의 (A급 — 단계 1 완료)

> **단계 1(핀포인트) 완료.** 착수 중 **드리프트 3(gRPC 모드 전량 유실)** 을 새로 발견해 함께 고쳤다.
> 단계 2(PARAM_SPEC 단일 소스)는 [../ROADMAP.md](../ROADMAP.md) Phase 4에 남아 있다.

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

## 드리프트 (3) — gRPC 모드는 필터 파라미터가 시작 시 **전부** 유실 (착수 중 발견)

문서 작성 시 놓쳤던 것. 로컬 모드는 `use_chunk_size` 하나가 빠진 정도였지만,
**gRPC 모드는 전달 경로 자체가 없었다.**

- [models.py](../backend/app/routers/models.py)의 gRPC 분기가 `body.params` 에서 `task` 만 꺼내 썼다
  (로컬은 `**body.params`)
- `GRPC_CLIENT_ARGS_MAP` 에도 `grpc_wrapper` argparse 에도 필터 인자가 하나도 없었다
  (`--config-overrides` 자체가 없었다)
- `fps` 가 `20` 으로 하드코딩되어 UI 슬라이더를 무시했다

**티가 안 났던 이유**: `grpc_wrapper` 의 전역 기본값이 UI 기본값과 우연히 전부 일치했다
(max_velocity 180, lowpass_alpha 0.5, gripper_bypass_filter True …).
기본값으로 쓰면 멀쩡해 보이고, **값을 바꿔놓고 시작하면 무시되며, 한쪽 기본값만 바뀌면
조용히 어긋난다** — 이 폴더가 다루는 문제 유형 그대로다.

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

**결정: (b) 슬라이더 하나로 합침.** ACT 섹션의 `n_action_steps` 슬라이더를 제거하고
"받는 액션 청크 크기"(`use_chunk_size`) 하나로 통일했다. gRPC 모드에서 `n_action_steps` 는
어차피 쓰이지 않았고(서버에 요청할 개수는 별도 UI 필드 `actions_per_chunk`),
로컬 모드에서는 두 슬라이더가 같은 `_actions_per_chunk` 를 가리켰다.

## 완료 (단계 1)

| 변경 | 파일 |
|---|---|
| `override_keys` → 모듈 상수 `OVERRIDE_KEYS`, `use_chunk_size` 추가 | [cli_mapping.py](../backend/app/core/cli_mapping.py) |
| `build_grpc_client_args` 도 `--config-overrides` 를 싣도록 (`_split_overrides` 공유) | [cli_mapping.py](../backend/app/core/cli_mapping.py) |
| `max_velocity` 클램프 1000 → **500** (프론트 기준으로 통일) | [zmq_bridge.py](../backend/app/services/zmq_bridge.py) |
| gRPC 분기가 `**body.params` 전달, `fps` 하드코딩 제거 | [models.py](../backend/app/routers/models.py) |
| **미리보기/실행에 복붙돼 있던 인자 생성을 `_build_args_for()` 하나로** | [models.py](../backend/app/routers/models.py) |
| 시작 시 `use_chunk_size` 처리 (0이 아니면 `n_action_steps` 보다 우선) | [lerobot_wrapper.py](../wrapper/lerobot_wrapper.py) |
| `--config-overrides` 추가 + `apply_param()` 추출 (ZMQ와 시작 경로 공유) | [grpc_wrapper.py](../wrapper/grpc_wrapper.py) |
| `n_action_steps` 슬라이더 제거, `?? ` 폴백을 기본값과 일치 | [InferencePage.tsx](../frontend/src/pages/InferencePage.tsx) |

`_build_args_for()` 추출은 계획에 없던 것이다 — 같은 블록이 `/inference/preview` 와
`/inference/start` 에 복붙돼 있어서 **미리보기가 실제 실행과 다를 수 있었다**
(실제로 `fps` 하드코딩을 양쪽에 똑같이 고쳐야 했다). 사용자가 화면에서 확인한
CLI 명령이 거짓이 되는 구조라 합쳤다.

### ☑ 단계 2 완료 — 구조 통합

[app/core/inference_params.py](../backend/app/core/inference_params.py) 가 정본이고 나머지가 파생한다:

| 파생 대상 | 근거 |
|---|---|
| `zmq_bridge.SAFE_PARAMS` / `BOOL_PARAMS` | `realtime=True` |
| `cli_mapping.OVERRIDE_KEYS` | `send_at_start=True` |
| 프론트 슬라이더 범위·기본값 | `GET /api/params/spec` → [useParamSpec](../frontend/src/hooks/useParamSpec.ts) |

테스트가 **프론트에 min/max 하드코딩이 다시 생기면 실패**한다 (드리프트 2 재발 방지).

### 원래 계획 (참고)

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

자동 ([test_inference_params.py](../backend/tests/test_inference_params.py), 7개 — 전체 37개 통과):

- UI 파라미터 전량이 **두 모드 모두** 인자에 실리는지
- 두 모드가 **같은 오버라이드 집합**을 싣는지 (갈리면 모드마다 다르게 동작한다)
- gRPC `--fps` 가 하드코딩 20 이 아닌지
- `use_chunk_size` 가 `OVERRIDE_KEYS` 에 있는지 / `max_velocity` 상한이 500 인지
- **실시간 변경 가능한 값은 시작값도 반드시 전달되는지** —
  `(SAFE_PARAMS ∪ BOOL_PARAMS) - {fps, task} ⊆ OVERRIDE_KEYS`.
  한쪽에만 있으면 "슬라이더를 움직여야 적용되는 값"이 생긴다. 이게 원래 버그의 형태다

`cd frontend && npm run build` ☑

실기 확인이 남은 것 (하드웨어 필요, wrapper 를 고쳤으므로 필수):

- **로컬**: `use_chunk_size` 를 0이 아닌 값으로 두고 시작 → 로그에
  `actions_per_chunk: N (use_chunk_size)` 가 UI 값과 일치하는지
- **gRPC**: 시작 로그에 `Updated max_velocity/lowpass_alpha/...` 가 UI 값으로 찍히는지
  (이전에는 아무것도 안 찍혔다)
- 옛 `n_action_steps` 가 localStorage 에 남은 브라우저에서 시작해도 정상 동작하는지
- 슬라이더 전 항목을 극단값으로 밀어보고 백엔드 클램프 로그 확인

## 상태

☑ 완료 (단계 1·2) — 실기 검증 대기
