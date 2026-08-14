"""정책 레지스트리 — "지원하는 정책 타입"의 단일 정의.

이전에는 같은 목록이 6곳에 따로 적혀 있었고 **전부 다른 집합**이었다
(refactor/02-policy-registry.md). 실제 사고:

- `sac` 를 학습 화면에서 고를 수 있는데 추론 시작에서 `ValueError` 로 죽었다
- `pi0_fast` 는 추론에만 있고 학습에는 없었다
- Hub 태깅 목록에 정책이 아닌 `rtc` 가 섞여 있었고,
  `pi0` 가 `pi05` 보다 앞이라 **pi05 모델이 pi0 로 잘못 태깅**됐다

## 이제 정의는 `policies/*.yaml` 에 있다

여기는 **로더**다. 파이썬 dict 로 들고 있었더니 프론트가 못 읽어서, 화면 쪽에
`POLICY_TRAIN_SCHEMAS`(400줄)와 `policyType === '...'` 분기가 따로 자랐다
(feature/policy-ui-spec.md). 같은 사실을 프론트도 읽을 수 있는 곳에 두면
그 분기가 생길 자리가 없다.

`POLICIES` 의 **모양과 공개 함수는 그대로다** — 부르는 쪽은 한 줄도 안 바뀐다.

`core/` 에 두는 이유: 순수 데이터라 `services/` 를 참조하지 않는다
(`core/` → `services/` 방향 import 는 이 저장소에 없다).
"""

from typing import TypedDict

from app.core.policy_spec import PolicySpec, load_specs


class Policy(TypedDict, total=False):
    label: str
    supported: bool
    train: bool
    infer: bool
    rtc: bool
    language: bool
    encoder_probe: bool
    policy_base: str
    vlm_base: str


SPECS: dict[str, PolicySpec] = load_specs()


def _flatten(spec: PolicySpec) -> Policy:
    """YAML 스펙을 옛 `Policy` 모양으로. **부르는 쪽을 안 바꾸려고** 있는 함수다."""
    c = spec.capabilities
    return Policy(
        label=spec.label, supported=spec.supported,
        train=c.train, infer=c.infer, rtc=c.rtc,
        language=c.language, encoder_probe=c.encoder_probe,
        policy_base=spec.bases.policy, vlm_base=spec.bases.vlm,
    )


# ⚠ 정렬 순서가 화면 목록 순서다. YAML 파일명이 아니라 **원하는 표시 순서**로
# 고정한다 — 파일명 알파벳순이면 SmolVLA 가 ACT 뒤로 가서 목록이 뒤집힌다.
_ORDER = ["smolvla", "act", "diffusion", "pi0", "pi05", "pi0_fast", "vqbet", "tdmpc"]

POLICIES: dict[str, Policy] = {
    name: _flatten(SPECS[name])
    for name in sorted(SPECS, key=lambda n: (_ORDER.index(n) if n in _ORDER else 99, n))
}


def _names(flag: str) -> list[str]:
    """플래그가 켜진 **지원 정책**만. 미지원은 어느 목록에도 안 나온다.

    지금은 ACT·SmolVLA 만 지원한다 — 나머지는 정의를 남겨둔 채 꺼져 있다.
    한 정책을 되살리려면 `"supported": True` 한 줄이면 된다.
    """
    return [
        name for name, spec in POLICIES.items()
        if spec.get(flag) and spec.get("supported")
    ]


def supported() -> list[str]:
    return [name for name, spec in POLICIES.items() if spec.get("supported")]


def trainable() -> list[str]:
    return _names("train")


def inferable() -> list[str]:
    return _names("infer")


def rtc_policies() -> list[str]:
    return _names("rtc")


def takes_language(policy_type: str) -> bool:
    """이 정책이 `task` 문자열을 실제로 쓰는가.

    ACT 는 관측→행동만이라 task 를 줘도 안 쓴다. 그런데 **주면 화면에는 뜬다** —
    CLI 미리보기와 wrapper 로그에 `--task=...` 가 찍혀서, 입력란도 없는데
    "Task: Pick up the doll and put in the box" 같은 옛 문자열이 나타난다.
    입력을 감추는 것만으로는 부족하고 **보내는 쪽도 막아야 한다.**

    모르는 정책은 받는 쪽으로 본다 — 언어를 쓰는데 안 보내면 정책이 헛돈다.
    """
    spec = POLICIES.get(policy_type)
    return True if spec is None else bool(spec.get("language"))


def encoder_probe_policies() -> set[str]:
    return set(_names("encoder_probe"))


# Hub 모델 이름에서 정책 타입을 추론할 때 쓰는 순서.
# **긴 이름이 먼저**여야 한다 — "pi0" 가 앞에 오면 "pi05_base" 가 pi0 로 잡힌다.
# 정책이 아닌 것(예: rtc 스무딩 기법)은 레지스트리에 없으므로 자동으로 빠진다.
TAG_MATCH_ORDER: list[str] = sorted(POLICIES, key=len, reverse=True)


def guess_from_name(model_name: str) -> str | None:
    """Hub 모델 이름에서 정책 타입 추론. 못 찾으면 None."""
    lowered = model_name.lower()
    return next((p for p in TAG_MATCH_ORDER if p in lowered), None)


def spec_for_frontend() -> list[dict]:
    """GET /api/policies — 프론트가 목록·라벨·RTC 여부를 여기서 받는다."""
    return [
        {
            "type": name,
            "label": spec.get("label", name),
            "train": bool(spec.get("train")),
            "infer": bool(spec.get("infer")),
            "rtc": bool(spec.get("rtc")),
            "language": bool(spec.get("language")),
            "encoder_probe": bool(spec.get("encoder_probe")),
            # 처음부터 학습이 무의미한 정책의 권장 시작점.
            # 프론트가 따로 목록을 만들면 또 갈라진다.
            "policy_base": spec.get("policy_base", ""),
        }
        for name, spec in POLICIES.items()
        if spec.get("supported")
    ]


def ui_spec(policy_type: str) -> dict:
    """`GET /api/policies/{type}/ui` — 화면이 그릴 **항목**을 준다.

    ⚠ 배치는 안 담는다. 어느 카드에 몇 열로 놓을지는 화면이 정하고, 여기는
    "어떤 필드가 있고 범위·기본값이 얼마인가"만 말한다. 그 선을 넘기 시작하면
    타입 검사도 디버깅도 안 되는 JSX 를 YAML 로 쓰게 된다.
    """
    spec = SPECS.get(policy_type)
    if spec is None:
        # 모르는 정책이면 빈 스펙. **막지는 않는다** — 스펙은 편의 계층이지
        # 안전 계층이 아니다. 값 검증의 정본은 백엔드 클램프에 남아 있다.
        return {"type": policy_type, "scratch_note": "",
                "train": {"defaults": {}, "fields": [], "warnings": []},
                "encoder_probe": {"base_label": "", "taps": [],
                                  "image_key_select": False, "note": ""}}
    t = spec.train
    return {
        "type": policy_type,
        "scratch_note": spec.scratch_note,
        "train": {
            "defaults": t.defaults,
            "fields": [
                {
                    "key": f.key,
                    # 라벨을 안 적으면 키를 그대로 쓴다 — LeRobot 설정 이름이
                    # 곧 사용자가 검색할 이름이라 대개 그게 낫다.
                    "label": f.label or f.key,
                    "kind": f.kind,
                    "default": f.resolved_default(),
                    "min": f.min, "max": f.max, "step": f.step,
                    "arch": f.arch,
                    # 상류와 일부러 다른 값이면 화면이 그 이유를 보여줄 수 있다
                    "override_reason": f.override.reason if f.override else "",
                }
                for f in t.fields
            ],
            "warnings": [
                {
                    "when": {"field": w.when.field, "is": w.when.is_},
                    "and": ({"field": w.and_.field, "is": w.and_.is_} if w.and_ else None),
                    "level": w.level, "text": w.text,
                }
                for w in t.warnings
            ],
        },
        "encoder_probe": {
            "base_label": spec.encoder_probe.base_label,
            "taps": [{"key": t_.key, "label": t_.label, "default": t_.default}
                     for t_ in spec.encoder_probe.taps],
            "image_key_select": spec.encoder_probe.image_key_select,
            "note": spec.encoder_probe.note,
        },
    }
