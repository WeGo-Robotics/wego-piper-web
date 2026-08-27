"""말단 속도 — 기구학은 `piper_robot` 에서 가져온다.

⚠ **기존 `speed` 와 다른 물건이다.** `fsm.compute_signals` 의 `speed` 는 관절
공간에서 잰다 — 관절 변화 벡터의 L2 노름이라 어깨 1도와 손목 1도를 같게 센다.
실제로 말단이 움직인 거리는 어깨 쪽이 훨씬 큰데도.

여기서는 말단 좌표의 궤적에서 속도를 낸다. 단위가 **m/s** 라 사람이 읽을 수 있고
관절 구성에 안 흔들린다.

## FK 는 여기 없다

`piper_robot.kinematics` 가 갖고 있다. 그쪽이 robotd 의 바닥 필터에도 쓰이므로
변환식이 두 벌이 되면 **분석과 안전이 서로 다른 팔을 본다.** 이 파일에는
페이즈 분석에만 필요한 것(도함수 창, 속도)만 남는다.

값의 사슬:

    정규화(-100..100) ──`joints.denormalize_joint`──▶ raw ──/1000──▶ 도 ──▶ 라디안 ──FK──▶ m
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

from piper_robot.kinematics import (  # noqa: F401  (재수출: 부르는 쪽 표면 유지)
    ARM_JOINTS,
    available,
    endpoint_xyz,
    norm_to_rad,
)

# 미분 창(프레임, 홀수)과 다항식 차수 — Savitzky–Golay.
#
# ⚠ **차분이 아니라 국소 다항식의 도함수를 쓴다.** 창 안의 점들에 2차 다항식을
#   맞추고 그 기울기를 취한다. 차분은 두 점만 보므로 그 두 점의 잡음이 그대로
#   들어가지만, 이쪽은 창 전체를 쓰면서도 곡률을 허용해 **봉우리를 안 깎는다.**
#
#   실측 (bolt_two1 ep1, 15fps) — 고주파 비중 / 봉우리:
#
#       1프레임 차분      19.1%  0.399 m/s
#       ±1 중심차분        8.4%  0.378        ← 이전
#       ±2 중심차분        4.6%  0.332
#       SG 창5 (2차)       2.7%  0.331        ← 지금. ±2 와 봉우리는 같은데 잡음 절반
#       SG 창7 (2차)       1.6%  0.310        ← 더 매끄럽지만 봉우리를 18% 깎는다
#
#   위치 궤적 자체의 고주파가 0.9% 이므로 2.7% 는 신호 고유값에 가깝다.
_SG_WINDOW = 5
_SG_ORDER = 2


@lru_cache(maxsize=8)
def _sg_kernel(window: int, order: int) -> np.ndarray:
    """국소 다항식의 **1차 계수**를 뽑는 합성곱 커널 (= 도함수)."""
    half = window // 2
    t = np.arange(-half, half + 1, dtype=float)
    basis = np.vander(t, order + 1, increasing=True)
    return np.linalg.pinv(basis)[1][::-1].copy()


def _sg_derivative(v: np.ndarray, fps: float) -> np.ndarray:
    """(T,3) 궤적 → (T,3) 속도 벡터. 가장자리는 끝값으로 채운다."""
    if len(v) < _SG_WINDOW:
        d = np.diff(v, axis=0, prepend=v[:1])
        return d * fps
    k = _sg_kernel(_SG_WINDOW, _SG_ORDER)
    half = _SG_WINDOW // 2
    pad = np.pad(v, ((half, half), (0, 0)), mode="edge")
    cols = [np.convolve(pad[:, i], k, mode="valid") for i in range(v.shape[1])]
    return np.stack(cols, axis=1) * fps


def endpoint_speed(state: np.ndarray, fps: float) -> np.ndarray:
    """정규화 관절값(T,7+) → 말단 속도(T,) m/s.

    **Savitzky–Golay 도함수**로 잰다 — 위 `_SG_WINDOW` 주석 참고.

    ⚠ 좌표를 먼저 구하고 **그 궤적을 미분한다.** 관절별로 미분해서 합치지 않는다 —
      둘은 수학적으로 같은 것을 재지만, 궤적 쪽이 한 번만 미분하면 되고
      야코비안을 매 프레임 만들 필요도 없다.

    ⚠ 그래도 완전히 매끈해지지는 않는다. **크기(norm)는 음수가 없어서 잡음이
      상쇄되지 않고 쌓인다** — 위치 잡음은 부호가 양쪽이라 평균이 0 이지만,
      길이로 바꾸는 순간 전부 양수가 되어 정지 중에도 몇 mm/s 가 남는다.
      실측: 정지 구간에서 평균 3.9mm/s. 나머지는 사람 손의 실제 떨림이다.

    첫 프레임은 0 이다 — `fsm.compute_signals` 의 `speed` 와 같은 규약이라
    두 신호를 같은 축에 겹쳐 볼 수 있다.

    ⚠ 관절 공간 `speed` 에는 같은 처리를 **안 했다.** 그쪽은 FSM 임계값이
      물려 있어서 매끄럽게 만들면 페이즈 경계가 통째로 움직인다. 이 신호는
      화면 표시 전용이라 안전하다.
    """
    xyz = endpoint_xyz(norm_to_rad(np.asarray(state, dtype=float)[:, :len(ARM_JOINTS)]))
    n = len(xyz)
    if n < 2:
        return np.zeros(n)
    out = np.linalg.norm(_sg_derivative(xyz, fps), axis=1)
    out[0] = 0.0
    return out
