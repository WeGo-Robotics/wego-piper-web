"""SO-101 리더 → Piper 팔로워 매핑 — 릴레이의 순수 계산 (feature/so101d.md §5).

두 모드가 여기의 순수 함수를 쓴다:

- **관절 매칭** (§5a): 축이 겹치는 관절끼리 정합(클러치) 기준 **변화량**을
  잇는다. 영점 규약·시작 자세 차이는 앵커가 흡수한다 — 남는 정적 설정은
  부호 테이블뿐이다.
- **말단 POSE** (§5b): 정규화 → URDF 라디안 (FK 입력). 스윕 캘리브레이션의
  중앙(2047)이 URDF 한계 중점과 정렬된다 — 실측: so101 한계 중점이 전 관절
  ≈0° 라 이 가정이 선다.

⚠ **정규화 공간에서 매핑하지 않는다.** 두 팔의 관절 범위가 달라 각도가
비선형으로 왜곡된다 (Piper 마스터 그리퍼 사고와 같은 병). 각도 공간이 정본.
"""

import math
from pathlib import Path

from piper_so101 import calibration as cal_mod
from piper_so101.joints import SO101_JOINTS

#: 관절쌍 (so101 → piper, 부호). Piper joint4(전완 롤)는 대응이 없어
#: **정합 시점 값 유지** — 조그로 미리 세팅해 두면 그대로 간다.
#: 부호는 실기 검증 대상 — 방향이 반대면 여기 한 곳만 뒤집는다.
PAIRS: tuple[tuple[str, str, float], ...] = (
    ("shoulder_pan", "joint1", +1.0),
    ("shoulder_lift", "joint2", +1.0),
    ("elbow_flex", "joint3", +1.0),
    ("wrist_flex", "joint5", +1.0),
    ("wrist_roll", "joint6", +1.0),
)

_RAD_PER_TICK = 2.0 * math.pi / 4096.0

#: shm 레코드 자리(joint1..) ← so101 이름 — piper_so101.joints 의 매핑과 동일
#: (그쪽은 dict 지만 여기서는 리더 레코드를 읽는 방향만 쓴다).
from piper_so101.joints import _RECORD_OF as _REC  # noqa: E402


def load_spans(arm_name: str, calib: str | None = None) -> dict[str, float]:
    """리더 캘리브레이션에서 관절별 스윕 폭(틱). norm→rad 변환의 재료다.

    캘리브레이션이 없으면 실패한다 — 폭을 모르면 각도를 모른고, 각도 없이
    변화량 텔레옵을 하면 배율이 틀린 채 팔로워가 움직인다.
    """
    path = cal_mod.find_calibration(calib or arm_name)
    if path is None:
        raise FileNotFoundError(
            f"{arm_name} 의 캘리브레이션이 없습니다 — 카드의 [캘리브레이션]을 "
            "먼저 완주하세요")
    cal = cal_mod.load_calibration(Path(path))
    return {n: float(cal[n].range_max - cal[n].range_min)
            for n in SO101_JOINTS}


def leader_rad(record_values: dict, spans: dict[str, float]) -> dict[str, float]:
    """리더 shm 레코드(정규화 -100..100) → 관절별 라디안 (중앙=0 기준).

    norm v 는 캘리브레이션 범위의 위치이므로 각도 = (v/200)·span·(2π/4096).
    중앙(2047)=0 이고, 실측으로 URDF 한계 중점도 ≈0° 라 FK 입력으로도 쓴다.
    """
    out: dict[str, float] = {}
    for name in SO101_JOINTS:
        if name == "gripper":
            continue
        v = float(record_values.get(_REC[name], 0.0))
        out[name] = (v / 200.0) * spans[name] * _RAD_PER_TICK
    return out


def map_joint_goal(lead_rad: dict[str, float], lead_anchor: dict[str, float],
                   follower_anchor_rad, pairs=PAIRS):
    """관절 매칭의 본식: 팔로워[j] = 앵커[j] + 부호 × (리더 − 리더앵커).

    `follower_anchor_rad` 는 Piper 6축 라디안 벡터(K.ARM_JOINTS 순).
    대응 없는 joint4 는 앵커 값이 그대로 남는다. 순수 함수 — 테스트가 여기를
    직접 민다.
    """
    import numpy as np

    goal = np.array(follower_anchor_rad, dtype=float).copy()
    idx = {"joint1": 0, "joint2": 1, "joint3": 2,
           "joint4": 3, "joint5": 4, "joint6": 5}
    for so_name, piper_j, sign in pairs:
        goal[idx[piper_j]] += sign * (lead_rad[so_name] - lead_anchor[so_name])
    return goal


def relative_target(t_lead, t_lead_anchor, t_follower_anchor):
    """POSE 정합의 본식: 목표 = T_f0 · (T_l0⁻¹ · T_l).

    리더의 자세 **변화량**을 팔로워 정합 자세 위에 얹는다 — 정합 순간
    상대가 항등이라 첫 목표 = 팔로워 현재 자세 (점프 0). 베이스 정렬 회전은
    항등 가정 (두 팔이 같은 방향으로 작업대를 본다) — 필요해지면 여기에 낀다.
    """
    import numpy as np

    return np.asarray(t_follower_anchor) @ (
        np.linalg.inv(np.asarray(t_lead_anchor)) @ np.asarray(t_lead))


def piper_norm_from_rad(q_rad) -> dict[str, float]:
    """Piper 6축 라디안 → 정규화 dict (joint1..joint6). 변환은 저장소 정본
    (`piper_robot.joints`)을 쓴다 — 여기서 식을 다시 적으면 캘리브레이션이
    두 벌이 된다. 릴레이(backend)와 녹화 플러그인(LeRobot 프로세스)이 같은
    함수를 쓰라고 여기(설치 패키지)에 둔다."""
    import numpy as np

    from piper_robot import kinematics as K
    from piper_robot.joints import normalize_joint

    return {name: float(normalize_joint(
                name, float(np.degrees(q_rad[i]) * K.MILLIDEG_PER_DEG)))
            for i, name in enumerate(K.ARM_JOINTS)}
