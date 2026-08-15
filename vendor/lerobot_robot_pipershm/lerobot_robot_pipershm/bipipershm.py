"""양팔 shm 프록시 — `bi_piper_follower_shm` / `bi_piper_leader_shm`.

`BiPiperFollower`/`BiPiperLeader` 의 합성 로직을 그대로 물려받고
**팔 클래스만 shm 프록시로 갈아끼운다** (`arm_class`/`arm_config_class`).
관측·액션 계약이 direct 양팔과 동일해야 하므로 그 외에는 아무것도
오버라이드하지 않는다 — pipershmfollower.py 의 원칙과 같다.

세그먼트는 팔마다 한 쌍(`piper.arm.<iface>.state|action`) — robotd 는
팔 단위라 양팔이어도 아무것도 몰라도 된다 (feature/bimanual.md §4).
"""

from dataclasses import dataclass

from lerobot.robots.config import RobotConfig
from lerobot.teleoperators.config import TeleoperatorConfig
from lerobot_robot_piper.bi_piper_follower import BiPiperFollower
from lerobot_robot_piper.bi_piper_leader import BiPiperLeader
from lerobot_robot_piper.config_bi_piper import BiPiperFollowerConfig, BiPiperLeaderConfig

from .config_pipershmfollower import PiperShmArmConfig, PiperShmFollowerConfig
from .config_pipershmleader import PiperShmLeaderArmConfig, PiperShmLeaderConfig
from .pipershmfollower import PiperShmFollower
from .pipershmleader import PiperShmLeader


@RobotConfig.register_subclass("bi_piper_follower_shm")
@dataclass(kw_only=True)
class BiPiperShmFollowerConfig(BiPiperFollowerConfig):
    # 중첩은 평면 팔 설정 — 등록형을 중첩하면 draccus 가 재귀한다 (config_piper.py)
    left_arm_config: PiperShmArmConfig
    right_arm_config: PiperShmArmConfig

    @property
    def type(self) -> str:
        return self.get_choice_name(self.__class__)


@TeleoperatorConfig.register_subclass("bi_piper_leader_shm")
@dataclass(kw_only=True)
class BiPiperShmLeaderConfig(BiPiperLeaderConfig):
    left_arm_config: PiperShmLeaderArmConfig
    right_arm_config: PiperShmLeaderArmConfig

    @property
    def type(self) -> str:
        return self.get_choice_name(self.__class__)


class BiPiperShmFollower(BiPiperFollower):

    config_class = BiPiperShmFollowerConfig
    name = "bi_piper_follower_shm"

    arm_class = PiperShmFollower
    arm_config_class = PiperShmFollowerConfig


class BiPiperShmLeader(BiPiperLeader):

    config_class = BiPiperShmLeaderConfig
    name = "bi_piper_leader_shm"

    arm_class = PiperShmLeader
    arm_config_class = PiperShmLeaderConfig
