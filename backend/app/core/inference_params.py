"""추론 파라미터 스펙 — 이름·범위·기본값·라벨의 단일 정의.

이전에는 파라미터 하나가 **네 곳**에 따로 적혀 있었다
(refactor/01-inference-params.md):

| 위치 | 무엇 |
|---|---|
| `InferencePage.tsx` 기본값 dict | default |
| `InferencePage.tsx` 슬라이더 | min/max/step/label |
| `param_bridge.SAFE_PARAMS` | 런타임 클램프 범위 |
| `cli_mapping.OVERRIDE_KEYS` | 시작 시 전달 여부 |

하나라도 빠지면 **에러 없이 조용히 값이 유실**됐다. 실제로 세 건이 어긋나 있었다.

이제 여기가 정본이고 나머지가 파생한다:

- `param_bridge.SAFE_PARAMS` / `BOOL_PARAMS`  → `realtime=True` 인 것
- `cli_mapping.OVERRIDE_KEYS`               → `send_at_start=True` 인 것
- 프론트 슬라이더                            → `GET /api/params/spec`

정책별 노출 여부는 **정책 파일이 정한다** — `policies/<type>.yaml` 의
`infer.extra_params`. 여기는 `scoped: True` 로 "전부는 아니다"만 표시한다.
"""

from typing import Literal, TypedDict

ParamKind = Literal["number", "bool"]


class ParamSpec(TypedDict, total=False):
    label: str
    kind: ParamKind
    default: float | bool
    min: float
    max: float
    step: float
    # 버스로 실시간 변경 가능 (= 기존 SAFE_PARAMS / BOOL_PARAMS)
    realtime: bool
    # 시작 시 --config-overrides 로 전달 (= 기존 OVERRIDE_KEYS)
    send_at_start: bool
    # **모든 정책에 해당하지 않는다**는 표시. 켜면 자기를 `infer.extra_params` 에
    # 적은 정책에서만 노출된다.
    #
    # ⚠ 여기에 정책 **이름**을 적지 않는다. 예전에는 `policies: ["smolvla","pi0","pi05"]`
    # 였는데, 그러면 모델을 추가할 때마다 **모두가 쓰는 이 표**를 고쳐야 한다.
    # 뒤집으면 새 모델은 자기 파일만 만들면 된다 (feature/policy-ui-spec.md 6단계).
    scoped: bool
    # UI 묶음 (프론트 카드 제목)
    group: str
    help: str


# ⚠ `fps` 와 `task` 는 CLI 인자로 따로 간다 (`--fps` / `--task`).
# 실시간 변경은 되지만 `send_at_start` 는 False — 시작값은 CLI 가 나른다.
PARAM_SPEC: dict[str, ParamSpec] = {
    # ── 실행 속도 ──
    "fps": {
        "label": "FPS", "kind": "number", "default": 20,
        "min": 1, "max": 60, "step": 1,
        "realtime": True, "send_at_start": False, "group": "실행 속도",
    },
    "max_velocity": {
        "label": "관절 속도 (deg/s)", "kind": "number", "default": 180,
        # 상한 500 — 프론트의 "관절 속도 제한(%)" 환산식이 500 기준이다.
        # 이전에는 백엔드만 1000이라 어느 쪽이 의도인지 불명확했다.
        "min": 0, "max": 500, "step": 10,
        "realtime": True, "send_at_start": True, "group": "실행 속도",
    },
    "gripper_bypass_filter": {
        "label": "그리퍼 속도 제한/필터 미적용 (원본 그대로)", "kind": "bool", "default": True,
        "realtime": True, "send_at_start": True, "group": "실행 속도",
    },
    "max_gripper_velocity": {
        "label": "그리퍼 속도 (%/s)", "kind": "number", "default": 300,
        "min": 0, "max": 500, "step": 10,
        "realtime": True, "send_at_start": True, "group": "실행 속도",
    },
    # ── 진동 감소 ──
    "lowpass_alpha": {
        "label": "저역통과 필터 α (1.0=OFF)", "kind": "number", "default": 0.5,
        "min": 0.05, "max": 1.0, "step": 0.05,
        "realtime": True, "send_at_start": True, "group": "진동 감소",
    },
    "max_jerk": {
        "label": "Jerk 제한 (deg/s², 0=OFF)", "kind": "number", "default": 0,
        "min": 0, "max": 5000, "step": 100,
        "realtime": True, "send_at_start": True, "group": "진동 감소",
    },
    "interpolation_steps": {
        "label": "보간 스텝 (0=OFF)", "kind": "number", "default": 0,
        "min": 0, "max": 10, "step": 1,
        "realtime": True, "send_at_start": True, "group": "진동 감소",
    },
    "use_chunk_size": {
        "label": "받는 액션 청크 크기 (0=모델 전체)", "kind": "number", "default": 0,
        "min": 0, "max": 200, "step": 5,
        "realtime": True, "send_at_start": True, "group": "진동 감소",
        "help": "로컬 모드에서는 n_action_steps 와 같은 값을 가리킨다 (슬라이더 통합됨)",
    },
    "refill_threshold_pct": {
        "label": "재추론 트리거 (큐 잔량 ≤ %, 0=소진 시)", "kind": "number", "default": 20,
        "min": 0, "max": 100, "step": 5,
        "realtime": True, "send_at_start": True, "group": "진동 감소",
    },
    # ── RTC (flow-matching 정책 전용) ──
    "max_guidance_weight": {
        "label": "max_guidance_weight", "kind": "number", "default": 10.0,
        "min": 0, "max": 50, "step": 0.5,
        "realtime": True, "send_at_start": True, "group": "RTC 파라미터",
        "scoped": True,
    },
    "execution_horizon": {
        "label": "execution_horizon", "kind": "number", "default": 10,
        "min": 1, "max": 100, "step": 1,
        "realtime": True, "send_at_start": True, "group": "RTC 파라미터",
        "scoped": True,
    },
    # ── ACT 전용 ──
    "temporal_ensemble_coeff": {
        "label": "temporal_ensemble_coeff", "kind": "number", "default": 0.01,
        "min": 0, "max": 1, "step": 0.001,
        "realtime": True, "send_at_start": True, "group": "ACT 파라미터",
        "scoped": True,
    },
    # 옛 localStorage 에 남아 들어올 수 있어 계약에는 남긴다.
    # UI 슬라이더는 use_chunk_size 로 통합됐다 (refactor/01 "노브 2개" 참고).
    "n_action_steps": {
        "label": "n_action_steps (사용 안 함 — use_chunk_size 로 통합)",
        "kind": "number", "default": 50, "min": 1, "max": 100, "step": 1,
        "realtime": True, "send_at_start": True, "group": "",
        "scoped": True,
    },
}


def _names(flag: str) -> set[str]:
    return {k for k, v in PARAM_SPEC.items() if v.get(flag)}


def realtime_params() -> set[str]:
    """버스로 실시간 변경 가능한 파라미터."""
    return _names("realtime")


def start_params() -> set[str]:
    """시작 시 `--config-overrides` 로 전달할 파라미터."""
    return _names("send_at_start")


def bool_params() -> set[str]:
    return {k for k, v in PARAM_SPEC.items() if v.get("kind") == "bool"}


def bounds() -> dict[str, dict[str, float]]:
    """런타임 클램프 범위. 숫자 파라미터만."""
    return {
        k: {"min": v["min"], "max": v["max"]}
        for k, v in PARAM_SPEC.items()
        if v.get("kind") != "bool" and "min" in v and "max" in v
    }


def defaults() -> dict[str, float | bool]:
    return {k: v["default"] for k, v in PARAM_SPEC.items() if "default" in v}


def _users_of(key: str) -> list[str]:
    """이 파라미터를 `infer.extra_params` 에 적은 정책들.

    ⚠ 응답 모양은 예전과 같다(`policies: [...]`) — 뒤집힌 건 **정본의 위치**지
    계약이 아니다. 덕분에 프론트는 한 줄도 안 바뀐다.

    지연 import 다: `policies` 가 이 모듈을 부르면 순환이 되는데, 지금은 안 부른다
    (테스트가 방향을 강제한다).
    """
    from app.core.policies import SPECS

    return sorted(n for n, spec in SPECS.items() if key in spec.infer.extra_params)


def spec_for_frontend() -> list[dict]:
    """GET /api/params/spec — 프론트가 슬라이더를 여기서 생성한다."""
    return [
        {
            "key": key,
            "label": v.get("label", key),
            "kind": v.get("kind", "number"),
            "default": v.get("default"),
            "min": v.get("min"),
            "max": v.get("max"),
            "step": v.get("step"),
            "group": v.get("group", ""),
            "policies": _users_of(key) if v.get("scoped") else [],
            "help": v.get("help", ""),
        }
        for key, v in PARAM_SPEC.items()
    ]
