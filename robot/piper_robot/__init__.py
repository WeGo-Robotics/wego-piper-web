"""Piper 팔 — **robotd 데몬의 본체**.

게이트웨이(`backend/`)를 import 하지 않는다. 데몬이 백엔드에 의존하면 분리한 의미가 없다.
rsd(`piper_rs`)·camerad(`piper_cam`)와 같은 구조다.

- `joints` — raw 엔코더 ↔ 정규화. **캘리브레이션의 단일 소유자**
- `safety` — 하드 리밋·데드맨. 순수 함수 (refactor/robotd-safety.md)
"""

from piper_robot.joints import (
    JOINT_CALIBRATION,
    JOINT_ORDER,
    denormalize_all,
    denormalize_joint,
    normalize_all,
    normalize_joint,
)
from piper_robot.safety import Reason, SafetyConfig, filter_goal

__all__ = [
    "JOINT_ORDER", "JOINT_CALIBRATION",
    "normalize_joint", "denormalize_joint", "normalize_all", "denormalize_all",
    "SafetyConfig", "Reason", "filter_goal",
]
