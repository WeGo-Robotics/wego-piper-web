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
    train: bool           # 학습 화면에서 고를 수 있는가
    infer: bool           # 추론 화면에서 고를 수 있는가 (= wrapper POLICY_IMPORTS 에 있어야 함)
    rtc: bool             # flow-matching → RTC 가이던스 파라미터 노출
    encoder_probe: bool   # 이미지 엔코더 프로브 지원
    policy_base: str      # Hub 베이스 체크포인트
    vlm_base: str         # VLM 백본


POLICIES: dict[str, Policy] = {
    "smolvla": {
        "label": "SmolVLA",
        "train": True, "infer": True, "rtc": True, "encoder_probe": True,
        "policy_base": "lerobot/smolvla_base",
        "vlm_base": "HuggingFaceTB/SmolVLM2-500M-Video-Instruct",
    },
    "act": {
        "label": "ACT",
        "train": True, "infer": True, "rtc": False, "encoder_probe": True,
    },
    "diffusion": {
        "label": "Diffusion",
        "train": True, "infer": True, "rtc": False, "encoder_probe": False,
    },
    "pi0": {
        "label": "π0",
        "train": True, "infer": True, "rtc": True, "encoder_probe": False,
        "policy_base": "lerobot/pi0_base",
        "vlm_base": "google/paligemma-3b-pt-224",
    },
    "pi05": {
        "label": "π0.5",
        "train": True, "infer": True, "rtc": True, "encoder_probe": False,
        "policy_base": "lerobot/pi05_base",
        "vlm_base": "google/paligemma-3b-pt-224",
    },
    "pi0_fast": {
        "label": "π0-FAST",
        "train": True, "infer": True, "rtc": False, "encoder_probe": False,
    },
    "vqbet": {
        "label": "VQ-BeT",
        "train": True, "infer": True, "rtc": False, "encoder_probe": False,
    },
    "tdmpc": {
        "label": "TD-MPC",
        "train": True, "infer": True, "rtc": False, "encoder_probe": False,
    },
    # `sac` 는 의도적으로 없다 — 강화학습(보상·환경·리플레이 버퍼)이라
    # 이 프로젝트의 수집→학습→추론 흐름과 맞지 않는다.
    # 되살리려면 wrapper/lerobot_wrapper.py 의 POLICY_IMPORTS 에도 함께 넣어야 한다.
}


def _names(flag: str) -> list[str]:
    return [name for name, spec in POLICIES.items() if spec.get(flag)]


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
            "encoder_probe": bool(spec.get("encoder_probe")),
        }
        for name, spec in POLICIES.items()
    ]
