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
        # ⚠ 말단 링크는 **팔마다 다르다.** Piper 는 `link6`(손목 플랜지), SO-101 은
        #   `gripper_frame_link` 다. 하드코딩하면 그 팔 하나에만 맞는다.
        #   옛 파일에는 없으므로 Piper 기본값으로 떨어진다.
        self.tip = str(npz["tip"]) if "tip" in npz.files else "link6"
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

    @property
    def tip_index(self) -> int:
        return self.names.index(self.tip)


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


def link_transforms(q_rad: np.ndarray, geom: "Geometry | None" = None) -> np.ndarray:
    """관절각 (T,N 라디안) → 링크별 자세 (T,L,4,4), base_link 기준.

    ⚠ `geom` 을 받는 이유: 리더와 팔로워가 **다른 팔**일 수 있다
    (`armmodel.ArmModel`). 예전에는 지오메트리가 모듈 전역 하나뿐이었는데,
    그 전제는 팔이 둘이 되는 순간 깨진다.
    """
    q = np.atleast_2d(np.asarray(q_rad, dtype=float))
    g = geom or geometry()
    dof = int((g.qidx >= 0).sum())
    if q.shape[1] != dof:
        raise ValueError(f"(T,{dof}) 이어야 합니다: {q.shape}")
    t = len(q)
    out = np.empty((t, len(g.names), 4, 4))
    for k in range(len(g.names)):
        local = g.fixed[k]
        qi = int(g.qidx[k])
        step = (local @ _about_axis(g.axis[k], q[:, qi])) if qi >= 0 else np.tile(local, (t, 1, 1))
        p = int(g.parent[k])
        out[:, k] = step if p < 0 else out[:, p] @ step
    return out


def endpoint_xyz(q_rad: np.ndarray, geom: "Geometry | None" = None) -> np.ndarray:
    """말단 좌표 (T,3 m). 말단은 `link6` 원점 — 손목 플랜지다.

    그리퍼 끝이 아닌 이유는 **여닫으면 움직이기 때문**이다. 팔의 이동을 재는
    기준으로는 오히려 나쁘다. (바닥 검사는 그리퍼까지 본다 — `lowest_z`.)
    """
    g = geom or geometry()
    return link_transforms(q_rad, g)[:, g.tip_index, :3, 3]


def lowest_z(q_rad: np.ndarray, geom: "Geometry | None" = None) -> np.ndarray:
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

# 손목 특이점 판정. **위 짐벌락과 다른 물건이다.**
#
#   짐벌락      RPY **표현**의 문제 — pitch 가 ±90°면 roll·yaw 를 나눌 수 없다
#   손목 특이점 **기구학**의 문제 — joint5 가 0 이면 joint4 와 joint6 의 축이
#               겹쳐(실측 사이각 0.00°) 같은 자세를 두 관절 어느 쪽으로도 낼 수 있다
#
# 둘은 거의 겹치지 않는다: joint5≈0 인 자세 3000개 중 짐벌락 가드가 잡은 것은
# **0.2%** 뿐이었다. 따로 봐야 한다.
#
# 왜 위험한가 — 팔로워의 IK 가 joint4/joint6 분배를 **자유롭게** 고른다.
# 리더가 이 구간을 지나면 팔로워가 손목을 홱 뒤집을 수 있는데, 자세는 거의
# 안 변하므로 **자세 기준 걸음 상한에 안 걸린다.** 실측 (joint4 +20°, joint6 -20° 뒤집기):
#
#       joint5     0°  →  자세 변화 0.00°   ← 완전히 공짜
#                  1°  →           0.41°
#                  5°  →           1.91°
#                 10°  →           3.42°   ← 여기를 경계로 잡는다
#                 20°  →           5.69°
#
# 10° 아래에서는 40°짜리 손목 뒤집기가 자세 3.4° 값도 안 된다 — 조작자는 거의
# 못 느끼는데 팔은 크게 돈다.
WRIST_SINGULAR_DEG = 10.0


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


def end_pose(q_rad: np.ndarray, geom: "Geometry | None" = None) -> dict[str, int]:
    """관절각 (6,) → 말단 자세 **SDK 단위**(0.001mm / 0.001도).

    `arm.read_end_pose()` 와 같은 형태라 그대로 `EndPoseCtrl` 에 넣을 수 있다.
    """
    g = geom or geometry()
    q = np.atleast_2d(np.asarray(q_rad, dtype=float))
    tf = link_transforms(q, g)[0, g.tip_index]
    x, y, z = tf[:3, 3] * 1000.0                     # m → mm
    roll, pitch, yaw = rpy_from_matrix(tf[:3, :3])
    return {
        "x": int(round(x * UM_PER_MM)), "y": int(round(y * UM_PER_MM)),
        "z": int(round(z * UM_PER_MM)),
        "rx": int(round(roll * MDEG_PER_DEG)), "ry": int(round(pitch * MDEG_PER_DEG)),
        "rz": int(round(yaw * MDEG_PER_DEG)),
    }


def near_wrist_singularity(q_rad: np.ndarray) -> bool:
    """joint5 가 0 근처인가 — joint4 와 joint6 이 같은 축이 되는 자리.

    거기서는 **같은 말단 자세를 두 관절 어느 쪽으로도 낼 수 있다.** 팔로워의
    온보드 IK 가 리더와 다른 쪽을 고를 수 있고, 지나가는 동안 분배가 바뀌면
    손목이 홱 돈다. 자세는 거의 안 변하므로 걸음 상한이 못 잡는다.
    """
    q = np.atleast_1d(np.asarray(q_rad, dtype=float)).ravel()
    return abs(math.degrees(q[4])) < WRIST_SINGULAR_DEG


def near_gimbal_lock(q_rad: np.ndarray, geom: "Geometry | None" = None) -> bool:
    """이 자세에서 rx·rz 분해가 불안정한가.

    불안정한 구간에서 나온 rx·rz 를 MoveP 목표로 보내면 **팔이 홱 돈다** —
    사람은 리더를 조금 움직였을 뿐인데. 그럴 때는 보내지 않는 편이 낫다.
    """
    g = geom or geometry()
    q = np.atleast_2d(np.asarray(q_rad, dtype=float))
    tf = link_transforms(q, g)[0, g.tip_index]
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, -float(tf[2, 0])))))
    return abs(abs(pitch) - 90.0) < GIMBAL_MARGIN_DEG


# ── 역기구학 ────────────────────────────────────────────────────────────────
#
# ## 왜 수치해인가
#
# Piper 는 **구형 손목이 아니다** — joint6 원점이 joint4·5 에서 91mm 떨어져 있어
# (실측) Pieper 분해가 안 된다. 해석해를 쓰려면 이 팔 전용으로 유도해야 한다.
#
# 그런데 이 IK 를 만드는 이유가 **다른 팔(SO-101 등)을 팔로워로 붙이는 것**이다.
# 팔마다 유도하면 그 목적이 사라진다. 사슬만 주면 도는 수치해가 맞다.
#
# ## 이어짐(continuity)이 정확도만큼 중요하다
#
# 직전 해에서 출발한다. 그러면 같은 가지(branch)에 머물러 **손목이 홱 뒤집히지
# 않는다** — 팔의 온보드 IK 를 쓸 때 못 막던 바로 그 문제다(joint5≈0 에서
# joint4/joint6 분배가 자유로워 자세는 그대로인데 관절이 40도 도는 것).

#: 감쇠 최소자승의 감쇠 계수. 특이점 근처에서 해가 폭주하는 것을 막는다.
#: 크면 안정적이고 느리며, 작으면 빠르고 특이점에서 튄다.
IK_DAMPING = 0.05
IK_MAX_ITERS = 60
IK_TOL_MM = 0.5
IK_TOL_DEG = 0.3


def _joint_axes_and_origins(tf: np.ndarray, geom: "Geometry | None" = None
                            ) -> tuple[np.ndarray, np.ndarray]:
    """움직이는 관절들의 월드 회전축과 원점. `tf` 는 `link_transforms` 한 프레임."""
    g = geom or geometry()
    ks = [k for k in range(len(g.names)) if int(g.qidx[k]) >= 0]
    axes = np.stack([tf[k, :3, :3] @ g.axis[k] for k in ks])
    origins = np.stack([tf[k, :3, 3] for k in ks])
    return axes, origins


def jacobian(q_rad: np.ndarray, geom: "Geometry | None" = None) -> np.ndarray:
    """말단(link6)의 기하 야코비안 (6,N). 위 3행 선속도, 아래 3행 각속도."""
    g = geom or geometry()
    tf = link_transforms(np.atleast_2d(q_rad), g)[0]
    axes, origins = _joint_axes_and_origins(tf, g)
    p_e = tf[g.tip_index, :3, 3]
    lin = np.cross(axes, p_e - origins)
    return np.vstack([lin.T, axes.T])


def _pose_error(tf_now: np.ndarray, target: np.ndarray) -> np.ndarray:
    """현재 → 목표의 6D 오차 (m, rad). 회전은 축각으로 낸다 —
    오일러각 차분은 짐벌 근처에서 **크기가 뻥튀기된다.**"""
    err = np.empty(6)
    err[:3] = target[:3, 3] - tf_now[:3, 3]
    r = target[:3, :3] @ tf_now[:3, :3].T
    angle = math.acos(max(-1.0, min(1.0, (np.trace(r) - 1.0) / 2.0)))
    if angle < 1e-9:
        err[3:] = 0.0
    else:
        axis = np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]])
        err[3:] = axis / (2.0 * math.sin(angle)) * angle
    return err


def pose_matrix(pose: dict) -> np.ndarray:
    """SDK 단위 6D 자세 dict → 4x4. `end_pose` 의 역이다."""
    t = np.eye(4)
    t[:3, 3] = [pose["x"] / UM_PER_MM / 1000.0, pose["y"] / UM_PER_MM / 1000.0,
                pose["z"] / UM_PER_MM / 1000.0]
    r, p, y = (math.radians(pose[k] / MDEG_PER_DEG) for k in ("rx", "ry", "rz"))
    cr, sr, cp, sp, cy, sy = (math.cos(r), math.sin(r), math.cos(p),
                              math.sin(p), math.cos(y), math.sin(y))
    t[:3, :3] = [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp,     cp * sr,                cp * cr],
    ]
    return t


def ik(target: np.ndarray, seed: np.ndarray, limits: np.ndarray | None = None,
       *, geom: "Geometry | None" = None, weights: np.ndarray | None = None,
       damping: float = IK_DAMPING, max_iters: int = IK_MAX_ITERS,
       tol_mm: float = IK_TOL_MM, tol_deg: float = IK_TOL_DEG) -> dict:
    """목표 4x4 → 관절각. **`seed` 에서 출발한다** — 그게 이어짐을 만든다.

    `weights` 는 6D 오차의 축별 가중 (위치3 + 자세3). **자유도가 6 미만인 팔에서
    무엇을 포기할지 정하는 자리다** — SO-101 은 5축이라 임의 6D 를 원리적으로 못
    맞춘다(실측: 무가중이면 잔차 중앙 190mm/10°). 위치를 무겁게 주면 위치를 맞추고
    자세를 양보한다. `None` 이면 균등.

    반환: `{"ok", "q", "iters", "pos_mm", "rot_deg"}`.
    `ok` 가 False 면 허용오차 안에 못 들어온 것이다 — 그 자세를 못 가는 것이지
    코드가 고장난 것이 아니다. 부르는 쪽이 그 구분을 사용자에게 전해야 한다.
    """
    g = geom or geometry()
    idx = g.tip_index
    q = np.array(seed, dtype=float).copy()
    lim = limits if limits is not None else joint_limits()
    w = np.ones(6) if weights is None else np.asarray(weights, dtype=float)
    for i in range(max_iters):
        tf = link_transforms(q[None, :], g)[0, idx]
        err = _pose_error(tf, target)
        pos_mm = float(np.linalg.norm(err[:3]) * 1000.0)
        rot_deg = float(math.degrees(np.linalg.norm(err[3:])))
        if pos_mm < tol_mm and rot_deg < tol_deg:
            return {"ok": True, "q": q, "iters": i, "pos_mm": pos_mm, "rot_deg": rot_deg}
        j = jacobian(q, g)
        # 감쇠 최소자승: (JᵀJ + λ²I)⁻¹ Jᵀ e. 특이점에서 pinv 가 폭주하는 것을 막는다.
        # 가중은 오차와 야코비안 양쪽에 같이 건다 — 한쪽만 걸면 최소화하는 것이
        # 가중 오차가 아니게 되어 가중이 방향만 바꾸고 크기는 안 바꾼다.
        jw, ew = j * w[:, None], err * w
        dq = jw.T @ np.linalg.solve(jw @ jw.T + (damping ** 2) * np.eye(6), ew)
        q = np.clip(q + dq, lim[:, 0], lim[:, 1])
    tf = link_transforms(q[None, :], g)[0, idx]
    err = _pose_error(tf, target)
    return {"ok": False, "q": q, "iters": max_iters,
            "pos_mm": float(np.linalg.norm(err[:3]) * 1000.0),
            "rot_deg": float(math.degrees(np.linalg.norm(err[3:])))}


# IK 가 관절 한계 밖으로 이만큼은 나가도 된다.
#
# ⚠ **실제 팔은 URDF 한계 밖에 앉아 있다.** 실측(두 대, 접힌 자세):
#
#       joint3   +2.9°  (한계 -170~0)
#       joint2   -0.6°  (한계 0~180)
#
# 기계적 스토퍼와 명목 사양이 정확히 같지 않아서다. 여유 없이 잘라내면 **리더가
# 지금 서 있는 자세를 IK 가 못 푼다** — 실제로 그래서 "IK 가 목표에 못 닿았습니다
# (12mm 남음)" 가 떴다.
#
# 이 여유는 **해를 표현하게 해줄 뿐**이고, 실제로 나가는 명령은 robotd 의
# `filter_goal` 이 정규화 ±100 으로 다시 자른다. 안전 경계를 넓히는 것이 아니다.
IK_LIMIT_MARGIN_DEG = 5.0


@lru_cache(maxsize=1)
def joint_limits() -> np.ndarray:
    """관절 한계 (N,2 라디안) + 위 여유. URDF 값에서 온다."""
    urdf = np.array([(-2.6179938, 2.6179938), (0.0, 3.1415926), (-2.9670597, 0.0),
                     (-1.7453292, 1.7453292), (-1.2217304, 1.2217304),
                     (-2.0943951, 2.0943951)])
    m = math.radians(IK_LIMIT_MARGIN_DEG)
    return urdf + np.array([-m, m])
