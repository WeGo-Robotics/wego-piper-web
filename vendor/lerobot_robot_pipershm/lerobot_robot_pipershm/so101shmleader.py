"""SO-101 리더 텔레오퍼레이터 — shm 읽기 전용 + 관절 매칭 (feature/so101d.md §5).

`PiperShmLeader` 와 같은 자리에 서되 `get_action()` 이 다르다: 리더 상태를
그대로 돌려주는 게 아니라 **정합(클러치) 기준 변화량을 Piper 관절에 매핑**해
돌려준다. 정합은 `connect()` 순간이다 — 그때의 리더·팔로워 자세가 앵커라
첫 액션이 곧 팔로워 현재 자세다(점프 0). 녹화 중 재정합은 v1 에 없다.

두 세그먼트를 **읽기만** 한다 — 명령 세그먼트는 로봇 클래스(PiperShmFollower)
가 만든다. 여기서 만들면 라이터가 둘이 된다.
"""

import logging
from typing import Any

import numpy as np
from lerobot.teleoperators.teleoperator import Teleoperator
from lerobot.utils.errors import DeviceNotConnectedError
from piper_shm import ArmSegmentError, StateReader

from .config_so101shmleader import So101ShmLeaderConfig
from .motor_specs import MOTORS

logger = logging.getLogger(__name__)

#: 리더 상태가 이보다 묵으면 액션을 내지 않는다 (릴레이 STALE_S 와 같은 뜻)
STALE_S = 0.5


class So101ShmLeader(Teleoperator):

    config_class = So101ShmLeaderConfig
    name = "so101_leader_shm"

    def __init__(self, config: So101ShmLeaderConfig):
        super().__init__(config)
        self.config = config
        self.port = config.port
        self._leader: StateReader | None = None
        self._follower: StateReader | None = None
        self._spans: dict[str, float] | None = None
        self._l_anchor: dict[str, float] | None = None
        self._f_anchor_rad = None
        self._last: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.name}({self.port} → {self.config.follower})"

    @property
    def action_features(self) -> dict:
        # 액션은 **팔로워(Piper) 관절계**다 — 학습·추론이 그대로 맞는다
        return {f"{motor}.pos": float for motor in MOTORS}

    @property
    def feedback_features(self) -> dict:
        return {}

    @property
    def is_connected(self) -> bool:
        return self._leader is not None and self._follower is not None

    def connect(self, calibrate: bool = True) -> None:
        from piper_robot import kinematics as K
        from piper_so101 import relay_map

        try:
            self._leader = StateReader(self.port)
            self._follower = StateReader(self.config.follower)
        except ArmSegmentError as exc:
            self.disconnect()
            raise ConnectionError(f"{self}: 세그먼트를 열 수 없습니다 — {exc}") from exc
        # 폭을 모르면 각도를 모른다 — 캘리브레이션 없이는 시작하지 않는다
        self._spans = relay_map.load_spans(self.port, self.config.calib or None)
        lead = self._leader.read()
        foll = self._follower.read()
        if lead is None or foll is None:
            self.disconnect()
            raise ConnectionError(f"{self}: 리더/팔로워가 아직 상태를 발행하지 않습니다")
        # 정합 — 지금의 양쪽 자세가 앵커
        self._l_anchor = relay_map.leader_rad(lead["values"], self._spans)
        self._f_anchor_rad = K.norm_to_rad(np.array(
            [[foll["values"][j] for j in K.ARM_JOINTS]], float))[0]
        logger.info("%s connected — 정합 완료 (read-only)", self)

    @property
    def is_calibrated(self) -> bool:
        return True

    def calibrate(self) -> None:
        pass

    def configure(self) -> None:
        pass

    def get_action(self) -> dict[str, Any]:
        from piper_so101 import relay_map

        if not self.is_connected:
            raise DeviceNotConnectedError(f"{self} is not connected.")
        rec = self._leader.read()
        if rec is None:
            raise ConnectionError(f"{self}: 리더가 상태를 발행하지 않습니다")
        if self._leader.age_s() > STALE_S:
            # 얼어붙은 리더를 계속 밀면 팔로워는 그게 의도인 줄 안다 —
            # 직전 액션을 되풀이해 그 자리에 세운다
            if self._last is not None:
                return dict(self._last)
            raise ConnectionError(f"{self}: 리더 상태가 묵었습니다 (so101d 가 살아 있나요?)")
        lead = relay_map.leader_rad(rec["values"], self._spans)
        goal_rad = relay_map.map_joint_goal(lead, self._l_anchor, self._f_anchor_rad)
        goal = relay_map.piper_norm_from_rad(goal_rad)
        goal["gripper"] = float(rec["values"].get("gripper", 0.0))   # 절대 통과
        self._last = {f"{k}.pos": v for k, v in goal.items()}
        return dict(self._last)

    def send_feedback(self, feedback: dict[str, Any]) -> None:
        pass

    def disconnect(self) -> None:
        for r in (self._leader, self._follower):
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass
        self._leader = self._follower = None
