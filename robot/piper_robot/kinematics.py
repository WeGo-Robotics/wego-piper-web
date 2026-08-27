"""팔 기구학 — 관절각 → 링크 자세, 그리고 **팔의 가장 낮은 점**.

## 왜 여기인가

바닥 필터는 [`safety.filter_goal`](safety.py) 안에서 돌고, 그건 CAN 을 쥔
robotd 안이다. 기구학이 캘리브레이션과 **같은 프로세스**에 있어야 한다는 것이
refactor/robotd-safety.md 의 결론이다 — FK 는 관절 0점과 방향에 전적으로
의존하므로, 둘이 갈라지면 *안전하다고 판단하고 바닥을 친다.*

`piper_phase.kinematics` 도 이걸 쓴다. 변환식이 두 벌이 되면 어긋난다.

## 런타임에 URDF 를 안 읽는다

지오메트리는 `data/arm_geometry.npz` 에 구워져 있다
(`tools/build_arm_geometry.py`). robotd 는 호스트에 가볍게 배포되고 배포 절차에
서브모듈 단계가 없다. 드리프트는 `test_arm_geometry.py` 가 잡는다.

## 근사 — 링크당 구 덮개

메시 충돌은 안 한다. 링크 정점을 1cm 복셀로 뭉치고 각 점에 반지름
`cell·√3/2 = 0.87cm` 를 준다. **덮개는 항상 실제보다 아래를 본다** — 틀리는
방향이 안전한 쪽으로 고정돼 있고, 오차 상한이 자세와 무관하다(바운딩 박스는
자세에 따라 0.4~4.1cm 로 출렁인다).
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import numpy as np

from piper_robot.joints import denormalize_joint

DATA = Path(__file__).resolve().parent / "data" / "arm_geometry.npz"

# 팔 사슬의 관절 순서. 데이터셋 `observation.state` 의 앞 6축과 같다.
ARM_JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")

# raw 단위가 밀리도라는 것은 추측이 아니다: `JOINT_CALIBRATION` 의 범위를 1000 으로
# 나누면 URDF 관절 한계와 joint1~4 가 정확히 일치한다 (±150°, 0~180°, -170~0°, ±100°).
MILLIDEG_PER_DEG = 1000.0


class Geometry:
    """구운 지오메트리. 읽기 전용이고 프로세스당 한 번 로드된다."""

    def __init__(self, npz) -> None:
        self.names: tuple[str, ...] = tuple(str(n) for n in npz["names"])
        self.parent = npz["parent"]
        self.xyz = npz["xyz"]
        self.rpy = npz["rpy"]
        self.axis = npz["axis"]
        self.qidx = npz["qidx"]
        self.pts = npz["pts"].astype(np.float64)
        self.pt_link = npz["pt_link"]
        self.radius = float(npz["radius"])
        # 부모→자식 고정 변환은 관절값과 무관하다 — 한 번 만들어 둔다
        self.fixed = np.stack([_fixed(self.xyz[i], self.rpy[i])
                               for i in range(len(self.names))])
        # ⚠ **뿌리 링크는 바닥 검사에서 뺀다.** 팔이 볼트로 고정된 바로 그 면이라
        #   바닥면을 "위반"할 수 있는 물건이 아니다. 넣으면 영자세부터 걸린다 —
        #   base_link 메시가 z=0 에서 시작하는데 복셀 중심과 반지름이 그걸
        #   1.37cm 아래로 내려 읽기 때문이다.
        #   실측(관절 범위 안 무작위 4000자세): base_link 의 최저 z 변동이
        #   **정확히 0.00cm** 다. 자세로 움직일 수 없는 것은 명령으로 부딪칠 수도 없다.
        root = int(np.flatnonzero(self.parent < 0)[0])
        self.movable = self.pt_link != root
        # 링크별로 점을 미리 갈라 둔다. 점마다 변환을 gather 하면 (T,N,3) 짜리
        # 큰 배열이 생기는데, 링크별 작은 곱 11번이 그보다 여섯 배 빠르다
        # (실측 0.92ms → 0.33ms, 30fps 제어 주기의 2.8% → 1.0%).
        self.by_link = [(k, self.pts[self.pt_link == k])
                        for k in range(len(self.names))
                        if k != root and (self.pt_link == k).any()]

    def index(self, name: str) -> int:
        return self.names.index(name)


@lru_cache(maxsize=1)
def geometry() -> Geometry:
    with np.load(DATA, allow_pickle=False) as z:
        return Geometry(z)


def available() -> bool:
    """지오메트리를 읽을 수 있나. 패키지에 들어 있으므로 보통 True 다."""
    return DATA.is_file()


def _fixed(xyz, rpy) -> np.ndarray:
    """xyz 이동 + rpy 회전. URDF 규약은 R = Rz·Ry·Rx."""
    r, p, y = rpy
    cr, sr, cp, sp, cy, sy = (math.cos(r), math.sin(r), math.cos(p),
                              math.sin(p), math.cos(y), math.sin(y))
    t = np.eye(4)
    t[:3, :3] = [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp,     cp * sr,                cp * cr],
    ]
    t[:3, 3] = xyz
    return t


def _about_axis(axis, q: np.ndarray) -> np.ndarray:
    """축 회전 (T,4,4). Piper 는 전부 z 축이지만 URDF 를 그대로 따른다 —
    다른 팔이나 갱신된 파일에서 축이 바뀌어도 조용히 틀리지 않게."""
    n = np.linalg.norm(axis)
    x, y, z = (np.asarray(axis) / n) if n else (0.0, 0.0, 1.0)
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


def link_transforms(q_rad: np.ndarray) -> np.ndarray:
    """관절각 (T,6 라디안) → 링크별 자세 (T,L,4,4), base_link 기준."""
    q = np.atleast_2d(np.asarray(q_rad, dtype=float))
    if q.shape[1] != len(ARM_JOINTS):
        raise ValueError(f"(T,{len(ARM_JOINTS)}) 이어야 합니다: {q.shape}")
    g = geometry()
    t = len(q)
    out = np.empty((t, len(g.names), 4, 4))
    for k in range(len(g.names)):
        local = g.fixed[k]
        qi = int(g.qidx[k])
        step = (local @ _about_axis(g.axis[k], q[:, qi])) if qi >= 0 else np.tile(local, (t, 1, 1))
        p = int(g.parent[k])
        out[:, k] = step if p < 0 else out[:, p] @ step
    return out


def endpoint_xyz(q_rad: np.ndarray) -> np.ndarray:
    """말단 좌표 (T,3 m). 말단은 `link6` 원점 — 손목 플랜지다.

    그리퍼 끝이 아닌 이유는 **여닫으면 움직이기 때문**이다. 팔의 이동을 재는
    기준으로는 오히려 나쁘다. (바닥 검사는 그리퍼까지 본다 — `lowest_z`.)
    """
    g = geometry()
    return link_transforms(q_rad)[:, g.index("link6"), :3, 3]


def lowest_z(q_rad: np.ndarray) -> np.ndarray:
    """팔 전체에서 **가장 낮은 점**의 높이 (T,), base_link 기준 m.

    그리퍼까지 포함한다 — 바닥에 먼저 닿는 것이 그리퍼다. 덮개 반지름을 빼므로
    실제보다 **낮게** 나온다: 틀리는 방향이 안전한 쪽이다.
    """
    g = geometry()
    tf = link_transforms(q_rad)
    # 점의 월드 z = (그 링크 회전의 z행)·p + (그 링크 원점의 z)
    out = np.full(len(tf), np.inf)
    for k, pts in g.by_link:
        z = tf[:, k, 2, :3] @ pts.T + tf[:, k, 2, 3][:, None]
        np.minimum(out, z.min(axis=1), out=out)
    return out - g.radius


def norm_to_rad(state: np.ndarray) -> np.ndarray:
    """정규화 관절값 (T,6) → 라디안.

    변환은 저장소의 정본(`joints.denormalize_joint`)을 그대로 쓴다. 여기서 식을
    다시 적으면 캘리브레이션이 바뀔 때 한쪽만 고쳐진다.
    """
    s = np.asarray(state, dtype=float)
    if s.ndim != 2 or s.shape[1] < len(ARM_JOINTS):
        raise ValueError(f"(T,{len(ARM_JOINTS)}+) 이어야 합니다: {s.shape}")
    raw = np.empty((len(s), len(ARM_JOINTS)))
    for i, name in enumerate(ARM_JOINTS):
        raw[:, i] = [denormalize_joint(name, v) for v in s[:, i]]
    return np.radians(raw / MILLIDEG_PER_DEG)


# ── 말단 6D 자세 (텔레오퍼레이션 POSE 모드) ─────────────────────────────────
#
# ⚠ **리더 팔은 말단 자세를 안 보낸다.** 마스터로 설정된 팔은 피드백(0x2Ax)을
#   내지 않아 `GetArmEndPoseMsgs` 가 0,0,0,0,0,0 을 돌려준다(실측). 그래서
#   리더의 자세는 FK 로 구하는 수밖에 없다.
#
#   다행히 그래도 된다 — 팔로워에서 대조한 결과 우리 FK 가 팔이 스스로 보고하는
#   값과 **위치 0.08mm, 자세 0.00°** 로 맞는다. 원점(link6 플랜지)도 회전 규약
#   (RPY = Rz·Ry·Rx)도 같다.

UM_PER_MM = 1000.0        # SDK 선형 단위 0.001mm
MDEG_PER_DEG = 1000.0     # SDK 각 단위 0.001도

# 짐벌락 판정. |pitch| 가 90°에 이만큼 가까우면 rx·rz 로 나누는 것이 불안정해진다 —
# 관절이 조금 움직여도 두 값이 크게 튀고, 그게 MoveP 명령이 되면 팔이 홱 돈다.
GIMBAL_MARGIN_DEG = 5.0


def rpy_from_matrix(rot: np.ndarray) -> tuple[float, float, float]:
    """회전행렬 → (roll, pitch, yaw) 도. URDF·팔과 같은 `Rz·Ry·Rx` 규약이다."""
    pitch = math.asin(max(-1.0, min(1.0, -float(rot[2, 0]))))
    if abs(math.cos(pitch)) > 1e-8:
        roll = math.atan2(rot[2, 1], rot[2, 2])
        yaw = math.atan2(rot[1, 0], rot[0, 0])
    else:                       # 짐벌락 — roll·yaw 를 나눌 수 없다. 하나로 몰아준다.
        roll = math.atan2(-rot[1, 2], rot[1, 1])
        yaw = 0.0
    return tuple(math.degrees(v) for v in (roll, pitch, yaw))


def end_pose(q_rad: np.ndarray) -> dict[str, int]:
    """관절각 (6,) → 말단 자세 **SDK 단위**(0.001mm / 0.001도).

    `arm.read_end_pose()` 와 같은 형태라 그대로 `EndPoseCtrl` 에 넣을 수 있다.
    """
    q = np.atleast_2d(np.asarray(q_rad, dtype=float))
    tf = link_transforms(q)[0, geometry().index("link6")]
    x, y, z = tf[:3, 3] * 1000.0                     # m → mm
    roll, pitch, yaw = rpy_from_matrix(tf[:3, :3])
    return {
        "x": int(round(x * UM_PER_MM)), "y": int(round(y * UM_PER_MM)),
        "z": int(round(z * UM_PER_MM)),
        "rx": int(round(roll * MDEG_PER_DEG)), "ry": int(round(pitch * MDEG_PER_DEG)),
        "rz": int(round(yaw * MDEG_PER_DEG)),
    }


def near_gimbal_lock(q_rad: np.ndarray) -> bool:
    """이 자세에서 rx·rz 분해가 불안정한가.

    불안정한 구간에서 나온 rx·rz 를 MoveP 목표로 보내면 **팔이 홱 돈다** —
    사람은 리더를 조금 움직였을 뿐인데. 그럴 때는 보내지 않는 편이 낫다.
    """
    q = np.atleast_2d(np.asarray(q_rad, dtype=float))
    tf = link_transforms(q)[0, geometry().index("link6")]
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, -float(tf[2, 0])))))
    return abs(abs(pitch) - 90.0) < GIMBAL_MARGIN_DEG
