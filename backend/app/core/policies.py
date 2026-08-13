"""정책 레지스트리 — "지원하는 정책 타입"의 단일 정의.

이전에는 같은 목록이 6곳에 따로 적혀 있었고 **전부 다른 집합**이었다
(refactor/02-policy-registry.md). 실제 사고:

- `sac` 를 학습 화면에서 고를 수 있는데 추론 시작에서 `ValueError` 로 죽었다
- `pi0_fast` 는 추론에만 있고 학습에는 없었다
- Hub 태깅 목록에 정책이 아닌 `rtc` 가 섞여 있었고,
  `pi0` 가 `pi05` 보다 앞이라 **pi05 모델이 pi0 로 잘못 태깅**됐다

`core/` 에 두는 이유: 순수 데이터라 `services/` 를 참조하지 않는다
(`core/` → `services/` 방향 import 는 이 저장소에 없다).
"""

from typing import TypedDict


class Policy(TypedDict, total=False):
    label: str
    # 지금 실제로 지원하는가. **정의는 지우지 않고 꺼둔다** —
    # 스키마·베이스 체크포인트·백본 안전망이 이미 붙어 있어서,
    # 나중에 이 한 줄만 켜면 되살아난다.
    supported: bool
    train: bool           # 학습 화면에서 고를 수 있는가
    infer: bool           # 추론 화면에서 고를 수 있는가 (= wrapper POLICY_IMPORTS 에 있어야 함)
    rtc: bool             # flow-matching → RTC 가이던스 파라미터 노출
    # 언어 지시(task)를 입력으로 받는가. **VLA 만 받는다.**
    # ⚠ `vlm_base` 유무로 유추하지 않는다 — 둘은 다른 사실이고, VLM 백본 없이
    # 언어를 받는 정책이 나오면 그 순간 유추가 거짓말을 한다.
    language: bool
    encoder_probe: bool   # 이미지 엔코더 프로브 지원
    policy_base: str      # Hub 베이스 체크포인트
    vlm_base: str         # VLM 백본


POLICIES: dict[str, Policy] = {
    "smolvla": {
        "label": "SmolVLA",
        "supported": True,
        "train": True, "infer": True, "rtc": True, "encoder_probe": True,
        "language": True,
        "policy_base": "lerobot/smolvla_base",
        "vlm_base": "HuggingFaceTB/SmolVLM2-500M-Video-Instruct",
    },
    "act": {
        "label": "ACT",
        "supported": True,
        "train": True, "infer": True, "rtc": False, "encoder_probe": True,
        "language": False,      # ACT 는 관측→행동만. task 를 줘도 쓰이지 않는다
    },
    "diffusion": {
        "label": "Diffusion",
        "train": True, "infer": True, "rtc": False, "encoder_probe": False,
        "language": False,
    },
    "pi0": {
        "label": "π0",
        "train": True, "infer": True, "rtc": True, "encoder_probe": False,
        "language": True,
        "policy_base": "lerobot/pi0_base",
        "vlm_base": "google/paligemma-3b-pt-224",
    },
    "pi05": {
        "label": "π0.5",
        "train": True, "infer": True, "rtc": True, "encoder_probe": False,
        "language": True,
        "policy_base": "lerobot/pi05_base",
        "vlm_base": "google/paligemma-3b-pt-224",
    },
    "pi0_fast": {
        "label": "π0-FAST",
        "train": True, "infer": True, "rtc": False, "encoder_probe": False,
        "language": True,       # π0 계열 — RTC 는 안 쓰지만 언어는 받는다
    },
    "vqbet": {
        "label": "VQ-BeT",
        "train": True, "infer": True, "rtc": False, "encoder_probe": False,
        "language": False,
    },
    "tdmpc": {
        "label": "TD-MPC",
        "train": True, "infer": True, "rtc": False, "encoder_probe": False,
        "language": False,
    },
    # `sac` 는 의도적으로 없다 — 강화학습(보상·환경·리플레이 버퍼)이라
    # 이 프로젝트의 수집→학습→추론 흐름과 맞지 않는다.
    # 되살리려면 wrapper/lerobot_wrapper.py 의 POLICY_IMPORTS 에도 함께 넣어야 한다.
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
