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


def test_language_flag_is_explicit_for_every_policy():
    """언어 지시(task)를 받는가는 **정책마다 명시**해야 한다.

    `vlm_base` 유무로 유추하면 안 된다 — 둘은 다른 사실이고, VLM 백본 없이
    언어를 받는 정책이 나오면 그 순간 유추가 거짓말을 한다.
    빠뜨리면 `.get()` 이 조용히 False 로 떨어져 VLA 인데 task 입력이 사라진다.
    """
    from app.core.policies import POLICIES

    missing = [n for n, spec in POLICIES.items() if "language" not in spec]
    assert not missing, f"language 를 안 정한 정책: {missing}"


def test_vla_policies_take_language():
    """VLM 백본이 있으면 언어를 받는 게 정상이다 — 어긋나면 둘 중 하나가 틀렸다."""
    from app.core.policies import POLICIES

    for name, spec in POLICIES.items():
        if spec.get("vlm_base"):
            assert spec["language"], f"{name} 은 VLM 백본이 있는데 language=False 다"


def test_frontend_gets_the_language_flag():
    """화면이 임계 판정을 따로 하지 않게 백엔드가 사실을 내보낸다."""
    from app.core.policies import spec_for_frontend

    specs = {p["type"]: p for p in spec_for_frontend()}
    assert specs["act"]["language"] is False, "ACT 에 task 입력이 뜬다"
    assert specs["smolvla"]["language"] is True


def test_every_task_input_is_gated_on_the_policy():
    """**회귀** — 추론 화면에 task 입력이 두 군데 있었고 하나만 막았다.

    ACT 를 골라도 "시작 전 설정" 쪽 입력이 남아 있었다. 입력해도 쓰이지 않는데
    화면은 쓰이는 것처럼 보인다. 세 번째가 생겨도 여기서 잡힌다.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
           / "InferencePage.tsx").read_text()

    inputs = [m.start() for m in re.finditer(r"<input[^>]*value=\{taskText\}", src)]
    guards = [m.start() for m in re.finditer(r"takesLanguage\(activePolicy\)", src)]
    assert inputs, "task 입력을 못 찾았다 — 검사가 무의미해졌다"

    for pos in inputs:
        before = [g for g in guards if g < pos]
        assert before and (pos - before[-1]) < 600, (
            f"가드 밖에 있는 task 입력이 있다 (offset {pos})"
        )


def test_encoder_overlay_never_renders_at_a_mismatched_size():
    """**회귀** — 추출지점을 바꿔 다시 인코딩하면 화면이 하얘졌다.

    `new ImageData(data, w, h)` 는 길이가 `w*h*4` 와 다르면 RangeError 를 던진다.
    렌더 중 예외라 React 가 트리를 통째로 버린다 — 새로고침 전까지 흰 화면이다.
    추출지점이 바뀌면 격자 크기가 달라지는데, 그 사이 오버레이는 옛 크기다.

    두 겹으로 막는다: 슬롯이 바뀌면 오버레이를 **먼저 버리고**(비동기 재계산을
    기다리지 않는다), 그래도 어긋나면 **안 그린다**.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
           / "EncoderProbePage.tsx").read_text()

    assert "new ImageData(" in src, "검사 대상이 사라졌다 — 이 테스트가 무의미해졌다"
    assert "overlay.data.length === meta.grid_w * meta.grid_h * 4" in src, (
        "크기를 확인하지 않고 ImageData 를 만든다"
    )
    # 오버레이 재계산 effect 가 시작할 때 옛 것을 버리는가
    build = src.split("Promise.all([build('A'), build('B')])")[0]
    assert "setOverlays({ A: null, B: null })" in build[-600:], (
        "슬롯이 바뀌어도 옛 오버레이를 들고 있는다"
    )
