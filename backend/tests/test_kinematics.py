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
from piper_robot import kinematics as RK  # noqa: E402

pytestmark = pytest.mark.skipif(
    not K.available(), reason="URDF 서브모듈 없음 (git submodule update --init)")

ZERO = np.zeros((1, 6))


def test_the_chain_is_read_by_name_not_by_order():
    """URDF 가 갱신되며 순서가 바뀌어도 사슬이 조용히 뒤섞이면 안 된다 —
    뒤섞이면 말단 위치가 **그럴듯하게** 틀린다.

    사슬은 이제 `piper_robot` 이 갖고 있다 (robotd 의 바닥 필터와 같은 것을 써야
    분석과 안전이 같은 팔을 본다). 순서 검사는 그대로 유효하다.
    """
    g = RK.geometry()
    # 뿌리(base_link) 다음 6개가 팔 관절이고, 그 순서가 곧 q 벡터의 순서다
    moved = [g.names[i] for i in range(len(g.names)) if int(g.qidx[i]) >= 0]
    assert [int(g.qidx[i]) for i in range(len(g.names)) if int(g.qidx[i]) >= 0] == list(range(6))
    assert len(moved) == len(K.ARM_JOINTS)


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
    g = RK.geometry()
    reach = sum(float(np.linalg.norm(g.xyz[i])) for i in range(len(g.names))
                if int(g.qidx[i]) >= 0 or int(g.parent[i]) < 0)
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

    # 변환이 사는 곳이 `piper_robot.kinematics` 로 옮겨갔다 — 표를 베끼면 안
    # 된다는 규칙은 그 파일에 그대로 걸린다.
    src = (_REPO / "robot" / "piper_robot" / "kinematics.py").read_text()
    assert "from piper_robot.joints import denormalize_joint" in src
    used = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
    assert "JOINT_CALIBRATION" not in used, "캘리브레이션 표를 베껴 쓴다"


def test_the_geometry_needs_no_submodule():
    """서브모듈을 안 받은 체크아웃에서도 돌아야 한다.

    예전에는 URDF 를 런타임에 읽어서 없으면 이 신호가 통째로 빠졌다. 이제는
    지오메트리가 패키지에 구워져 있으므로 **항상 있다** — robotd 를 호스트에
    가볍게 배포하려고 그렇게 만든 것이고, 페이즈 분석이 덤으로 같이 얻는다.
    """
    assert K.available()


# ── 미분 방식 ───────────────────────────────────────────────────────────────

def test_frame_to_frame_jitter_is_cancelled():
    """⚠ **한 프레임 차분은 고역통과 필터다.**

    위치 자체는 매끄러운데(실측 고주파 성분 1%) 인접 두 프레임을 빼면 잡음
    바닥이 fps 배로 증폭돼 속도가 20% 까지 거칠어진다. 사용자가 "관절 그래프는
    부드러운데 말단만 진동이 심하다"고 본 것이 이것이다 — 실제로는 관절 **속도**도
    똑같이 거칠었고(20.3%), 비교 대상이 **위치**였다.

    실제 잡음은 프레임마다 부호가 뒤집히는 양자화 튐이다. 중심차분은 앞뒤를
    같이 보므로 그런 성분이 서로 지워진다 — 여기서 그걸 그대로 재현한다.
    """
    n = 200
    q = np.zeros((n, 7))
    q[:, 0] = 20.0                       # 가만히 있는 팔
    q[::2, 0] += 0.05                    # 한 프레임 걸러 튄다 (양자화 잡음)

    xyz = K.endpoint_xyz(K.norm_to_rad(q[:, :6]))
    naive = np.linalg.norm(np.diff(xyz, axis=0, prepend=xyz[:1]), axis=1) * 15
    centred = K.endpoint_speed(q, fps=15)

    assert naive[1:].mean() > 0, "잡음 모델이 아무것도 안 만든다"
    # ⚠ 안쪽만 본다. 첫·끝 프레임은 앞이나 뒤가 없어 중심을 못 잡으므로 단순
    #   차분으로 떨어진다 — 두 프레임 때문에 방식 전체를 부정할 일은 아니다.
    inner = centred[2:-2]
    assert inner.max() < naive[1:].mean() * 0.05, \
        f"튐이 안 지워진다 (중심 {inner.max():.5f} vs 단순 {naive[1:].mean():.5f})"


def test_the_edges_fall_back_instead_of_breaking():
    """앞뒤가 없는 프레임에서도 값이 나와야 한다 — 그래프에 구멍이 나면 안 된다."""
    q = np.zeros((4, 7))
    q[:, 0] = [0.0, 5.0, 10.0, 15.0]
    v = K.endpoint_speed(q, fps=15)
    assert len(v) == 4 and np.all(np.isfinite(v))
    assert v[0] == 0.0 and v[-1] > 0.0


def test_the_peaks_survive_the_smoothing():
    """잡음을 줄이려다 실제 봉우리를 깎으면 '빠르게 움직였다' 가 사라진다.

    실측에서 ±1 은 봉우리를 5% 안에서 보존했고 ±2 부터 16% 깎였다.
    """
    n = 120
    q = np.zeros((n, 7))
    q[:, 0] = np.concatenate([np.zeros(50), np.linspace(0, 60, 20), np.full(50, 60.0)])
    v = K.endpoint_speed(q, fps=15)
    xyz = K.endpoint_xyz(K.norm_to_rad(q[:, :6]))
    naive = np.linalg.norm(np.diff(xyz, axis=0, prepend=xyz[:1]), axis=1) * 15
    assert v.max() > naive.max() * 0.9, "봉우리가 너무 깎였다"


def test_the_joint_signal_was_left_alone():
    """⚠ 관절 공간 `speed` 는 FSM 임계값이 물려 있다 — 매끄럽게 만들면
    페이즈 경계가 통째로 움직인다. 표시 전용 신호만 손본다."""
    src = (_REPO / "phase" / "piper_phase" / "fsm.py").read_text()
    body = src.split("def compute_signals", 1)[1].split("\ndef ", 1)[0]
    assert "np.diff(joints, axis=0, prepend=joints[:1])" in body, "관절 속도 계산이 바뀌었다"


def test_the_sidecar_version_is_actually_read():
    """⚠ 예전에는 쓰기만 하고 **아무도 안 읽는** 필드였다.

    그래서 미분 방식을 바꿨을 때 이미 만들어둔 사이드카가 옛 값을 그대로
    내보냈고, 화면은 안 바뀐 것처럼 보였다.
    """
    router = (Path(__file__).resolve().parents[1] / "app" / "routers" / "phase.py").read_text()
    assert "SIDECAR_VERSION" in router, "버전을 안 읽는다"
    body = router.split("def _sidecar_stale", 1)[1].split("\ndef ", 1)[0]
    assert "except Exception:\n        return True" in body, \
        "버전을 못 읽을 때 최신이라고 낙관한다"


def test_stale_sidecars_recompute_display_signals_only():
    """페이즈 라벨은 사람이 손댔을 수 있다 — 표시 전용 신호만 다시 만든다."""
    router = (Path(__file__).resolve().parents[1] / "app" / "routers" / "phase.py").read_text()
    assert 'need_tip = stale or "tip_speed" not in e.columns' in router
    assert 'if not stale and "home_dist" in e.columns:' in router
    assert '"phase": e["phase"].tolist()' in router, "라벨까지 다시 계산한다"


def test_the_derivative_fits_a_polynomial_instead_of_subtracting_two_points():
    """⚠ 차분은 **두 점의 잡음이 그대로** 들어간다. 국소 다항식을 맞추면 창 전체를
    쓰면서도 곡률을 허용해 봉우리를 안 깎는다.

    실측(bolt_two1 ep1): ±2 중심차분과 SG 창5 는 봉우리가 같은데(0.332/0.331)
    고주파는 4.6% 대 2.7% 다 — 같은 값을 주고 잡음만 절반이다.
    """
    src = (_REPO / "phase" / "piper_phase" / "kinematics.py").read_text()
    assert "_sg_kernel" in src and "np.vander" in src, "다항식 적합이 아니다"
    body = src.split("def endpoint_speed", 1)[1]
    assert "_sg_derivative" in body, "속도가 SG 를 안 쓴다"


def test_the_kernel_differentiates_exactly_on_a_straight_line():
    """직선 궤적의 기울기는 정확히 나와야 한다 — 필터가 값을 줄이면 안 된다."""
    n, v = 60, np.zeros((60, 3))
    v[:, 0] = np.arange(n) * 0.01          # 프레임당 1cm
    d = K._sg_derivative(v, fps=15)
    assert d[10:-10, 0] == pytest.approx(0.01 * 15, rel=1e-9)


def test_a_short_trace_still_gets_a_derivative():
    """창보다 짧은 에피소드도 값이 나와야 한다 — 그래프에 구멍이 나면 안 된다."""
    v = np.zeros((3, 3)); v[:, 0] = [0.0, 0.01, 0.02]
    assert np.all(np.isfinite(K._sg_derivative(v, fps=15)))


def test_changing_the_computation_bumps_the_sidecar_version():
    """⚠ 안 올리면 이미 만들어둔 사이드카가 옛 값을 그대로 내보낸다 —
    실제로 한 번 그랬다."""
    src = (_REPO / "phase" / "piper_phase" / "labeler.py").read_text()
    assert "SIDECAR_VERSION = 3" in src
    assert "Savitzky" in src, "무엇이 바뀌어 올렸는지 안 적혀 있다"
