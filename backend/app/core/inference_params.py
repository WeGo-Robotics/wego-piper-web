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
    # ⚠ `interpolation_steps`(보간)는 뺐다 — **청크 정책에는 해롭다.** N스텝에 한 번만 목표를 집어
    #   거기서 멈췄다 가므로 15/N Hz 로 맥동하고(보간 5 에서 떨림 0.66→1.34mm, 피크 3Hz), 사이에 낀
    #   액션을 버려 잡으러 내려가는 짧은 하강이 빠진다. 그리퍼는 우회라 제때 닫혀 **도착 전에 집는다**
    #   (2026-10-28 실물). 스펙에 없으므로 localStorage·프리셋에 남은 값도 시작·실행 중 어느 쪽으로도
    #   안 실린다 — 화면만 감추면 안 보이는 데서 계속 돈다. 코드는 ActionFilter 에 남아 있다(기본 0).
    "use_chunk_size": {
        "label": "받는 액션 청크 크기 (0=모델 전체)", "kind": "number", "default": 0,
        "min": 0, "max": 200, "step": 5,
        "realtime": True, "send_at_start": True, "group": "진동 감소",
        "help": "로컬 모드에서는 n_action_steps 와 같은 값을 가리킨다 (슬라이더 통합됨)",
    },
    "refill_threshold_pct": {
        "label": "재추론 트리거 (큐 잔량 ≤ %, 0=소진 시)", "kind": "number", "default": 20,
        "min": 0, "max": 100, "step": 1,
        "realtime": True, "send_at_start": True, "group": "진동 감소",
    },
    # 청크 이어 붙이기 — wrapper/chunk_merge.py. 로컬 큐 모드에서만 돈다(시간 앙상블이 켜져 있으면
    # 앙상블이 이미 합친다). 기본 10·7 — 실물 3모델에서 말단 떨림 30~50%↓·속도 15~35%↑(2026-10-28).
    # 상한이 낮은 이유는 chunk_merge.py "상한을 낮게 둔 이유" — 크게 잡으면 모서리를 깎는다.
    # ⚠ 기본값·상한을 바꾸면 chunk_merge.DEFAULT_*·MAX_* 도 (테스트가 잡는다).
    "chunk_blend_steps": {
        "label": "청크 경계 크로스페이드 (스텝, 0=OFF)", "kind": "number", "default": 10,
        "min": 0, "max": 12, "step": 1,
        "realtime": True, "send_at_start": True, "group": "진동 감소",
        "help": "새 청크가 오면 갈아끼우지 않고, 남은 큐에서 새 청크로 이 스텝 수에 걸쳐 옮겨 간다. 경계에서 팔이 되돌아가며 떠는 것을 없앤다",
    },
    "chunk_smooth_window": {
        "label": "청크 스무딩 창 (스텝, 0=OFF)", "kind": "number", "default": 7,
        "min": 0, "max": 9, "step": 1,
        "realtime": True, "send_at_start": True, "group": "진동 감소",
        "help": "새 청크를 받을 때 남은 큐 전체를 지연 없이 다듬는다. 5 미만은 꺼짐, 짝수는 다음 홀수로 쓴다",
    },
    # wrapper/jump_repair.py — 정책과 무관하게 모든 청크에 건다(앙상블이 켜져 있어도). PIPER 팔에서만
    # 돈다(말단 위치가 필요). 켬이 기본이고 wrapper 기본값도 켬이라 화면 밖에서 띄워도 같다.
    "jump_repair": {
        "label": "순간이동 고쳐 잇기", "kind": "bool", "default": True,
        "realtime": True, "send_at_start": True, "group": "진동 감소",
        "help": "청크 안에서 팔 끝이 한 스텝에 너무 멀리 가는 곳(15Hz 에서 35mm 초과)을 매끈하게 이어 원래 계획에 합류시킨다. PIPER 만",
    },
    # ── RTC (flow-matching 정책 전용) ──
    # 스위치 — wrapper/rtc_feed.py. 예전엔 아래 두 슬라이더만 있었고 wrapper 가 RTC 를 부르지 않아
    # **처음부터 효과가 없었다.** ⚠ 꺼짐이 기본: 켜면 그 정책 추론의 청크가 바뀐다(시간 앙상블과 같은 이유).
    "rtc_enabled": {
        "label": "RTC 켜기", "kind": "bool", "default": False,
        "realtime": True, "send_at_start": True, "group": "RTC 파라미터",
        "scoped": True,
        "help": "새 청크를 만들 때 이전 청크의 남은 부분을 이어 받는다. 청크마다 계획이 달라져 생기는 2~5Hz 흔들림을 줄인다",
    },
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
    # 시간 앙상블(ACT 논문 temporal ensembling) — wrapper/temporal_ensemble.py. 켜면 매 스텝
    # 추론하고 같은 스텝을 예측한 청크들을 가중 평균한다. 로컬 추론에서만 돈다.
    # ⚠ 스위치가 계수와 따로다. 계수(0.01)는 예전부터 시작값으로 실려 왔으므로 계수만으로 켜면
    #   모든 ACT 추론의 동작이 한꺼번에 바뀐다.
    # 기본은 켬 (2026-10-29) — 실기(ACT-DINOv3)에서 청크 이어 붙이기만 쓸 때보다 말단 2-5Hz 가
    # 명령 −58%·실제 −38%, 작업 사이클은 같았다. wrapper 기본값은 정책을 안 가려 꺼짐이므로 화면 밖에서
    # 띄운 추론에는 시작 라우터가 채운다(routers/models.py). 계수 상한 근거는 temporal_ensemble.MAX_COEFF.
    "temporal_ensemble": {
        "label": "시간 앙상블 (매 스텝 추론)", "kind": "bool", "default": True,
        "realtime": True, "send_at_start": True, "group": "ACT 파라미터",
        "scoped": True,
        "help": "켜면 매 제어 스텝마다 추론하고, 같은 스텝을 예측한 청크들을 가중 평균해 보낸다. 재추론 트리거는 쓰지 않는다",
    },
    "temporal_ensemble_coeff": {
        "label": "temporal_ensemble_coeff", "kind": "number", "default": 0.01,
        "min": 0, "max": 0.05, "step": 0.001,
        "realtime": True, "send_at_start": True, "group": "ACT 파라미터",
        "scoped": True,
        "help": "클수록 먼저 나온 예측을 더 믿는다. 0 = 균등 평균, 원 논문 0.01. 0.01~0.025 가 실측 최적 — 크게 올리면 오히려 떤다",
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


def foreign_to(policy_type: str) -> set[str]:
    """이 정책이 쓰지 않는 정책별(`scoped`) 파라미터 — 시작할 때도 실행 중에도 싣지 않는다.

    화면은 다른 정책의 카드를 감추지만 **값은 localStorage·프리셋에 남아 계속 실려 온다.**
    ACT 에서 켠 시간 앙상블이 SmolVLA 추론에 실리면 매 스텝 추론이 되는데 화면엔 스위치조차
    안 보인다. 모르는 정책이면 정책별 파라미터를 하나도 안 싣는다 — wrapper 기본값으로 돈다.
    """
    return {k for k, v in PARAM_SPEC.items() if v.get("scoped") and policy_type not in _users_of(k)}


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
