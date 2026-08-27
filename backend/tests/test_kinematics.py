"""URDF 기반 말단 위치·속도 (`piper_phase.kinematics`).

에피소드 뷰어의 `speed` 는 **관절 공간** 값이다 — 관절 변화 벡터의 L2 노름이라
어깨 1도와 손목 1도를 같게 센다. 말단이 실제로 움직인 거리는 어깨 쪽이 훨씬
큰데도. 그래서 URDF(`vendor/agx_arm_urdf`)를 타고 말단 좌표를 구해 m/s 로 낸다.

검증은 **불변식**으로 한다. 골든 좌표를 박아두면 그 값이 맞는지는 아무도 모르고,
URDF 가 갱신될 때 갱신인지 회귀인지도 못 가른다.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "phase"))
sys.path.insert(0, str(_REPO / "robot"))

from piper_phase import kinematics as K  # noqa: E402

pytestmark = pytest.mark.skipif(
    not K.available(), reason="URDF 서브모듈 없음 (git submodule update --init)")

ZERO = np.zeros((1, 6))


def test_the_chain_is_read_by_name_not_by_order():
    """URDF 가 갱신되며 순서가 바뀌어도 사슬이 조용히 뒤섞이면 안 된다 —
    뒤섞이면 말단 위치가 **그럴듯하게** 틀린다."""
    chain = K.load_chain()
    assert [l.name for l in chain] == list(K.ARM_JOINTS)


def test_joint1_rotates_the_endpoint_about_the_base_axis():
    """joint1 은 base 의 z 축 회전이다 — 반지름과 높이가 보존돼야 한다.

    이게 깨지면 고정 변환(rpy)이나 축 해석이 틀린 것이다.
    """
    q = np.zeros((7, 6))
    q[:, 0] = np.linspace(-1.0, 1.0, 7)
    p = K.endpoint_xyz(q)
    r = np.hypot(p[:, 0], p[:, 1])
    assert np.allclose(r, r[0], atol=1e-9), "반지름이 변한다"
    assert np.allclose(p[:, 2], p[0, 2], atol=1e-9), "높이가 변한다"


def test_the_endpoint_stays_within_the_arm_reach():
    """링크 길이 합보다 멀리 갈 수는 없다 — 변환이 어긋나면 이 값이 폭주한다."""
    reach = sum(float(np.linalg.norm(l.xyz)) for l in K.load_chain())
    rng = np.random.default_rng(0)
    q = rng.uniform(-2.0, 2.0, size=(200, 6))
    d = np.linalg.norm(K.endpoint_xyz(q), axis=1)
    assert d.max() <= reach + 1e-9, f"도달 범위({reach:.3f}m)를 넘는다: {d.max():.3f}m"


def test_a_still_arm_has_zero_endpoint_speed():
    q = np.tile(np.array([10.0, 20.0, -30.0, 5.0, 0.0, 0.0, 50.0]), (5, 1))
    assert np.allclose(K.endpoint_speed(q, fps=15), 0.0)


def test_the_first_frame_is_zero_like_the_joint_signal():
    """두 신호를 같은 축에 겹쳐 보려면 규약이 같아야 한다."""
    rng = np.random.default_rng(1)
    q = rng.uniform(-50, 50, size=(6, 7))
    assert K.endpoint_speed(q, fps=15)[0] == 0.0


def test_speed_scales_with_frame_rate():
    """같은 궤적을 두 배 빠른 fps 로 재면 속도도 두 배다."""
    q = np.zeros((4, 7))
    q[:, 0] = [0, 5, 10, 15]
    a = K.endpoint_speed(q, fps=15)
    b = K.endpoint_speed(q, fps=30)
    assert np.allclose(b[1:], 2 * a[1:])


def test_the_shoulder_moves_the_endpoint_more_than_the_wrist():
    """⚠ **이 신호가 존재하는 이유다.**

    관절 공간 속도는 둘을 같게 센다. 말단 기준이면 어깨가 훨씬 크게 움직인다 —
    그 차이를 못 보면 "천천히 접근 중"과 "크게 휘두르는 중"이 같은 값이 된다.
    """
    step = 5.0
    base = np.zeros((2, 7))
    shoulder = base.copy(); shoulder[1, 0] = step      # joint1
    wrist = base.copy();    wrist[1, 5] = step         # joint6
    assert (K.endpoint_speed(shoulder, 15)[1]
            > K.endpoint_speed(wrist, 15)[1] * 2), "어깨와 손목이 비슷하게 나온다"


def test_the_calibration_is_not_copied_here():
    """정규화 변환이 두 벌이 되면 한쪽만 고쳐서 말단 위치가 조용히 틀린다 —
    `joints.py` 머리말이 경고하는 사고다.

    ⚠ 문자열로 뒤지지 않고 **AST 의 식별자**를 본다. 이 모듈은 왜 표를 안 베끼는지
      설명하려고 docstring 에서 그 이름을 언급한다 — `code_only` 는 `#` 주석만
      걷어내므로 docstring 은 그대로 남아 검사에 걸린다.
    """
    import ast

    src = (_REPO / "phase" / "piper_phase" / "kinematics.py").read_text()
    assert "from piper_robot.joints import denormalize_joint" in src
    used = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
    assert "JOINT_CALIBRATION" not in used, "캘리브레이션 표를 베껴 쓴다"


def test_a_missing_submodule_does_not_raise():
    """서브모듈을 안 받은 체크아웃에서 페이즈 분석 전체가 죽으면 안 된다."""
    assert K.available(Path("/nonexistent/piper.urdf")) is False
