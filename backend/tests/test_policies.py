"""정책 레지스트리 계약 (refactor/02-policy-registry.md).

원래 문제: "지원하는 정책 타입"이 6곳에 따로 적혀 있었고 **전부 다른 집합**이었다.
`sac` 은 학습 화면에서 고를 수 있는데 추론 시작에서 `ValueError` 로 죽었다.
"""

import re
from pathlib import Path

from app.core import policies

_REPO = Path(__file__).resolve().parents[2]
_WRAPPER = _REPO / "wrapper" / "lerobot_wrapper.py"


def _wrapper_policy_imports() -> set[str]:
    """wrapper 의 POLICY_IMPORTS 키 — wrapper 는 백엔드를 import 하지 않는다
    (크래시 격리). 그래서 소스에서 읽어 대조한다."""
    src = _WRAPPER.read_text()
    block = re.search(r"POLICY_IMPORTS = \{(.*?)\n\}", src, re.S)
    assert block, "POLICY_IMPORTS 를 못 찾았다"
    return set(re.findall(r'^\s*"([a-z0-9_]+)":', block.group(1), re.M))


def test_inferable_policies_exist_in_wrapper():
    """`infer: True` 인데 wrapper 에 없으면 **추론 시작에서 죽는다.**

    이게 `sac` 으로 실제로 났던 사고다.
    """
    missing = set(policies.inferable()) - _wrapper_policy_imports()
    assert not missing, (
        f"추론 가능하다고 선언됐지만 wrapper POLICY_IMPORTS 에 없다: {missing} "
        "→ 추론 시작 시 ValueError 로 죽는다"
    )


def test_rtc_policies_are_inferable():
    """RTC 파라미터를 노출하는데 추론이 안 되면 의미가 없다."""
    assert set(policies.rtc_policies()) <= set(policies.inferable())


def test_encoder_probe_policies_are_inferable():
    assert policies.encoder_probe_policies() <= set(policies.inferable())


def test_tag_match_order_is_longest_first():
    """짧은 이름이 먼저면 접두사가 삼킨다 — `pi0` 가 앞이면 `pi05_base` 가 pi0 로 잡혔다."""
    lengths = [len(p) for p in policies.TAG_MATCH_ORDER]
    assert lengths == sorted(lengths, reverse=True)


def test_guess_from_name_prefix_collision():
    """실제로 났던 오태깅."""
    assert policies.guess_from_name("pi05_base") == "pi05"
    assert policies.guess_from_name("lerobot/pi0_base") == "pi0"
    assert policies.guess_from_name("my_pi0_fast_ckpt") == "pi0_fast"


def test_guess_ignores_non_policies():
    """`rtc` 는 스무딩 기법이지 정책이 아니다. `sac` 은 지원하지 않는다."""
    assert policies.guess_from_name("rtc_experiment") is None
    assert policies.guess_from_name("sac_walker") is None


def test_every_policy_has_a_label():
    for name, spec in policies.POLICIES.items():
        assert spec.get("label"), f"{name} 에 label 이 없다"


def test_frontend_spec_shape():
    spec = policies.spec_for_frontend()
    assert spec and all(
        {"type", "label", "train", "infer", "rtc", "encoder_probe"} <= set(p) for p in spec
    )


def test_sac_is_not_offered():
    """강화학습이라 이 프로젝트의 수집→학습→추론 흐름과 맞지 않는다.
    되살리려면 wrapper POLICY_IMPORTS 에도 함께 넣어야 한다."""
    assert "sac" not in policies.POLICIES
