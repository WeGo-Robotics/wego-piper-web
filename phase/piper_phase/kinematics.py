"""URDF 로 관절값 → 말단 위치. 엔드포인트 기준 속도를 내기 위한 최소 FK.

⚠ **기존 `speed` 와 다른 물건이다.** `fsm.derive_signals` 의 `speed` 는 관절
공간에서 잰다 — 관절 변화 벡터의 L2 노름이라 어깨 1도와 손목 1도를 같게 센다.
실제로 말단이 움직인 거리는 어깨 쪽이 훨씬 큰데도.

여기서는 URDF 를 타고 내려가 말단 좌표를 구하고, 그 궤적의 속도를 낸다.
단위가 **m/s** 라 사람이 읽을 수 있고 관절 구성에 안 흔들린다.

값의 사슬 (전부 이 저장소에 이미 있는 것):

    정규화(-100..100)  ──`joints.denormalize_joint`──▶  raw
    raw                ──/1000──▶                       도
    도                 ──radians──▶                      라디안  ──FK──▶ m

raw 가 밀리도라는 것은 추측이 아니다: `JOINT_CALIBRATION` 의 범위를 1000으로
나누면 URDF 관절 한계와 **joint1~4 가 정확히 일치한다** (±150°, 0~180°, -170~0°,
±100°). joint5·6 은 우리 캘리브레이션이 의도적으로 더 좁거나 치우쳐 있다.

URDF 는 서브모듈(`vendor/agx_arm_urdf`)이다. **없으면 예외를 던지지 않는다** —
서브모듈을 안 받은 체크아웃에서 페이즈 분석 전체가 죽으면 안 되므로, 부르는 쪽이
`available()` 로 묻고 이 신호만 건너뛴다.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
DEFAULT_URDF = _REPO / "vendor" / "agx_arm_urdf" / "piper" / "urdf" / "piper_description.urdf"

# 이 사슬의 관절 순서. 데이터셋 `observation.state` 의 앞 6축과 같은 순서여야 한다.
ARM_JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")


@dataclass(frozen=True)
class Link:
    """부모 링크에서 자식 링크로 가는 고정 변환 + 그 자리의 회전축."""

    name: str
    xyz: tuple[float, float, float]
    rpy: tuple[float, float, float]
    axis: tuple[float, float, float]


def available(urdf: Path | None = None) -> bool:
    """URDF 를 읽을 수 있나. 서브모듈을 안 받았으면 False."""
    return (urdf or DEFAULT_URDF).is_file()


@lru_cache(maxsize=4)
def load_chain(urdf: Path | None = None) -> tuple[Link, ...]:
    """URDF 에서 `joint1..joint6` 사슬을 읽는다.

    ⚠ **URDF 에 적힌 순서를 믿지 않고 이름으로 찾는다.** 파일이 갱신되면서 순서가
    바뀌어도 사슬이 조용히 뒤섞이지 않게 — 뒤섞이면 말단 위치가 그럴듯하게 틀린다.
    """
    path = urdf or DEFAULT_URDF
    root = ET.parse(path).getroot()
    by_name = {j.get("name"): j for j in root.findall("joint")}
    out: list[Link] = []
    for name in ARM_JOINTS:
        j = by_name.get(name)
        if j is None:
            raise ValueError(f"URDF 에 {name} 이 없습니다: {path}")
        o = j.find("origin")
        a = j.find("axis")
        out.append(Link(
            name=name,
            xyz=_triple(o.get("xyz") if o is not None else None),
            rpy=_triple(o.get("rpy") if o is not None else None),
            axis=_triple(a.get("xyz") if a is not None else None, default=(0.0, 0.0, 1.0)),
        ))
    return tuple(out)


def _triple(s: str | None, default=(0.0, 0.0, 0.0)) -> tuple[float, float, float]:
    if not s:
        return default
    v = [float(x) for x in s.split()]
    return (v[0], v[1], v[2])


def _fixed(link: Link) -> np.ndarray:
    """부모→자식 고정 변환 (xyz 이동 + rpy 회전). URDF 규약은 R = Rz·Ry·Rx."""
    r, p, y = link.rpy
    cr, sr, cp, sp, cy, sy = (math.cos(r), math.sin(r), math.cos(p),
                              math.sin(p), math.cos(y), math.sin(y))
    rot = np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp,     cp * sr,                cp * cr],
    ])
    t = np.eye(4)
    t[:3, :3] = rot
    t[:3, 3] = link.xyz
    return t


def _about_axis(axis: tuple[float, float, float], q: np.ndarray) -> np.ndarray:
    """축 회전 (T,4,4). Piper 는 전부 z 축이지만 URDF 를 그대로 따른다 —
    다른 팔이나 갱신된 파일에서 축이 바뀌어도 조용히 틀리지 않게."""
    x, y, z = np.array(axis) / (np.linalg.norm(axis) or 1.0)
    c, s = np.cos(q), np.sin(q)
    C = 1 - c
    out = np.zeros((len(q), 4, 4))
    out[:, 0, 0] = x * x * C + c
    out[:, 0, 1] = x * y * C - z * s
    out[:, 0, 2] = x * z * C + y * s
    out[:, 1, 0] = y * x * C + z * s
    out[:, 1, 1] = y * y * C + c
    out[:, 1, 2] = y * z * C - x * s
    out[:, 2, 0] = z * x * C - y * s
    out[:, 2, 1] = z * y * C + x * s
    out[:, 2, 2] = z * z * C + c
    out[:, 3, 3] = 1.0
    return out


def endpoint_xyz(q_rad: np.ndarray, urdf: Path | None = None) -> np.ndarray:
    """관절각(T,6 라디안) → 말단 좌표(T,3 m), base_link 기준.

    말단은 `link6` 원점 — 손목 플랜지다. 그리퍼 변형(`piper_with_gripper`)에는
    링크가 더 있지만, 그리퍼 끝은 **여닫으면 움직이므로** 팔의 이동을 재는
    기준으로는 오히려 나쁘다.
    """
    q = np.asarray(q_rad, dtype=float)
    if q.ndim != 2 or q.shape[1] != len(ARM_JOINTS):
        raise ValueError(f"(T,{len(ARM_JOINTS)}) 이어야 합니다: {q.shape}")
    acc = np.tile(np.eye(4), (len(q), 1, 1))
    for i, link in enumerate(load_chain(urdf)):
        acc = acc @ _fixed(link) @ _about_axis(link.axis, q[:, i])
    return acc[:, :3, 3]


def norm_to_rad(state: np.ndarray) -> np.ndarray:
    """정규화 관절값(T,6) → 라디안.

    변환은 저장소의 정본(`piper_robot.joints`)을 그대로 쓴다. 여기서 식을 다시
    적으면 캘리브레이션이 두 벌이 되고, 한쪽만 고치면 말단 위치가 조용히 틀린다 —
    `joints.py` 머리말이 경고하는 바로 그 사고다.
    """
    from piper_robot.joints import denormalize_joint

    out = np.empty_like(np.asarray(state, dtype=float))
    for i, name in enumerate(ARM_JOINTS):
        raw = np.array([denormalize_joint(name, float(v)) for v in state[:, i]])
        out[:, i] = np.radians(raw / 1000.0)      # raw 는 밀리도
    return out


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


def endpoint_speed(state: np.ndarray, fps: float, urdf: Path | None = None) -> np.ndarray:
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
    xyz = endpoint_xyz(norm_to_rad(np.asarray(state, dtype=float)[:, :len(ARM_JOINTS)]), urdf)
    n = len(xyz)
    if n < 2:
        return np.zeros(n)
    out = np.linalg.norm(_sg_derivative(xyz, fps), axis=1)
    out[0] = 0.0
    return out
