"""SO-101 관절 이름 ↔ 팔 shm 레코드(7f) 자리 매핑 — **한 곳** (feature/so101d.md §2).

팔 shm 레코드는 위치 인덱스 7-float 이고 소비자는 piper 식 키
(joint1..joint6, gripper)로 읽는다. SO-101(관절 5+그리퍼)은 같은 레코드에
그대로 실린다:

    joint1 ← shoulder_pan     joint4 ← wrist_flex
    joint2 ← shoulder_lift    joint5 ← wrist_roll
    joint3 ← elbow_flex       joint6 ← 0 (미사용)
    gripper ← gripper

⚠ 축 의미가 같은 자리끼리 맞춘 것이 **아니다** — 그건 릴레이의 관절 매칭
테이블(§5a) 몫이다. 여기는 단지 "6개 값을 7칸에 싣는 순서"이고, 소비자는
capabilities 의 joint_names 로 진짜 이름을 안다.
"""

from typing import Final

#: 모터 ID 순서 그대로 (STS3215 데이지체인 1..6).
SO101_JOINTS: Final[tuple[str, ...]] = (
    "shoulder_pan", "shoulder_lift", "elbow_flex",
    "wrist_flex", "wrist_roll", "gripper",
)

#: 모터 이름 → 버스 ID. lerobot-calibrate 의 JSON 에도 같은 id 가 실려
#: 있어 attach 때 대조한다 — 배선이 바뀐 팔을 조용히 오독하지 않게.
MOTOR_IDS: Final[dict[str, int]] = {
    "shoulder_pan": 1, "shoulder_lift": 2, "elbow_flex": 3,
    "wrist_flex": 4, "wrist_roll": 5, "gripper": 6,
}

#: shm 레코드 키(piper 식) ← SO-101 이름. joint6 은 비워 둔다(0.0).
_RECORD_OF: Final[dict[str, str]] = {
    "shoulder_pan": "joint1", "shoulder_lift": "joint2", "elbow_flex": "joint3",
    "wrist_flex": "joint4", "wrist_roll": "joint5", "gripper": "gripper",
}
_SO101_OF: Final[dict[str, str]] = {v: k for k, v in _RECORD_OF.items()}


def to_record(norm: dict[str, float]) -> dict[str, float]:
    """SO-101 정규화 값 → shm 레코드 키. 빠진 관절은 0.0 (joint6 포함)."""
    out = {"joint1": 0.0, "joint2": 0.0, "joint3": 0.0, "joint4": 0.0,
           "joint5": 0.0, "joint6": 0.0, "gripper": 0.0}
    for name, value in norm.items():
        key = _RECORD_OF.get(name)
        if key is not None:
            out[key] = float(value)
    return out


def from_record(values: dict[str, float]) -> dict[str, float]:
    """shm 레코드 키 → SO-101 이름 (joint6 은 버린다)."""
    return {name: float(values[key]) for key, name in _SO101_OF.items()
            if key in values}
