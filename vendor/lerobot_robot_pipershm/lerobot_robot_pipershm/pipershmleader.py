"""`PiperLeader` 의 버스만 바꿔 끼운 프록시 텔레오퍼레이터 — **읽기 전용**.

`pipershmfollower.py` 와 같은 패턴(`__init__`만 오버라이드해 `PiperLeader` 를
그대로 재사용)이되, `get_action()` 도 오버라이드한다: 직접 드라이버는
`bus.get_control()`(마스터 모드의 관절 제어지령 피드백, 0x150~0x15F)을 읽지만
`PiperShmMotorsBus` 에 그 채널은 없다 — robotd 가 `read_joints_normalized()` 로
발행하는 상태(`bus.get_action()`)가 리더팔의 실제 관절 위치를 그대로 담고 있으므로
그걸 쓴다.

버스는 `read_only=True` 로 연다 — 명령 세그먼트를 만들지 않는다
(`shm_motors_bus.py` 의 docstring 참고: 만들면 robotd 데드맨이 사람이 움직이는
팔에 "현재 자세 유지" CAN 명령을 보낸다).
"""

import logging
from typing import Any

from lerobot.utils.errors import DeviceNotConnectedError
from lerobot_robot_piper.piper_leader import PiperLeader
from lerobot.teleoperators.teleoperator import Teleoperator

from .config_pipershmleader import PiperShmLeaderConfig
from .motor_specs import CALIBRATION, MOTORS
from .shm_motors_bus import PiperShmMotorsBus

logger = logging.getLogger(__name__)


class PiperShmLeader(PiperLeader):

    config_class = PiperShmLeaderConfig
    name = "piper_leader_shm"

    def __init__(self, config: PiperShmLeaderConfig):
        # ⚠ `PiperLeader.__init__` 을 부르지 않는다 — CAN 을 직접 여는
        # `PiperMotorsBus` 를 만들기 때문이다. 조부모(`Teleoperator`)를 직접
        # 부르고 버스만 바꿔 끼운다 (pipershmfollower.py 와 같은 이유).
        Teleoperator.__init__(self, config)
        self.config = config
        self.id = config.id
        self.port = config.port
        self.bus = PiperShmMotorsBus(
            id=config.id, port=config.port,
            motors=dict(MOTORS), calibration=dict(CALIBRATION),
            read_only=True,
        )

    def get_action(self) -> dict[str, Any]:
        if not self.is_connected:
            raise DeviceNotConnectedError(f"{self} is not connected.")
        raw = self.bus.get_action()
        return {f"{k}.pos": v for k, v in raw.items()}
