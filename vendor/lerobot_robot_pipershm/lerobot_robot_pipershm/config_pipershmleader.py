"""`teleop.type = "piper_leader_shm"` 등록.

`config_pipershmfollower.py` 와 같은 메커니즘 — 평면 팔 설정을 상속해
필드를 물려받는다. `deadman_ms` 는 없다: 리더팔은 명령 세그먼트를 만들지 않으므로
(`PiperShmMotorsBus(read_only=True)`) 데드맨이 적용될 대상이 없다.
"""

from dataclasses import dataclass

from lerobot.teleoperators.config import TeleoperatorConfig
from lerobot_robot_piper.config_piper_leader import PiperLeaderArmConfig


@dataclass(kw_only=True)
class PiperShmLeaderArmConfig(PiperLeaderArmConfig):
    """`port` 는 여기서도 **CAN 인터페이스 이름**이다 (`can0`) — 세그먼트 이름이
    거기서 나오므로 robotd 와 소비자가 같은 이름으로 만난다."""


@TeleoperatorConfig.register_subclass("piper_leader_shm")
@dataclass(kw_only=True)
class PiperShmLeaderConfig(TeleoperatorConfig, PiperShmLeaderArmConfig):

    @property
    def type(self) -> str:
        return self.get_choice_name(self.__class__)
