"""양팔 Piper 설정 — `robot.type = "bi_piper_follower"` / `teleop.type = "bi_piper_leader"`.

상류 `bi_so_follower` 와 같은 모양: 팔 하나짜리 설정 둘을 **중첩**해서 받는다.
CLI 에서는 `--robot.left_arm_config.port=can_follower1` 식으로 온다.
카메라도 팔 설정 안에 중첩된다 — 관측 키는 팔 접두사가 붙어
`left_wrist`/`right_wrist` 가 되고, 공용 카메라는 왼팔 소속으로 넣는다
(grpc 즉석 조립이 확립한 규약, feature/bimanual.md §2).

⚠ 중첩 타입은 **평면 팔 설정**(`PiperArmConfig`)이다 — 등록형
(`PiperFollowerConfig`)을 중첩하면 draccus 인자 등록이 무한 재귀한다
(config_piper.py 의 주석 참고).
"""

from dataclasses import dataclass

from lerobot.robots.config import RobotConfig
from lerobot.teleoperators.config import TeleoperatorConfig

from .config_piper import PiperArmConfig
from .config_piper_leader import PiperLeaderArmConfig


@RobotConfig.register_subclass("bi_piper_follower")
@dataclass(kw_only=True)
class BiPiperFollowerConfig(RobotConfig):
    left_arm_config: PiperArmConfig
    right_arm_config: PiperArmConfig

    @property
    def type(self) -> str:
        return self.get_choice_name(self.__class__)


@TeleoperatorConfig.register_subclass("bi_piper_leader")
@dataclass(kw_only=True)
class BiPiperLeaderConfig(TeleoperatorConfig):
    left_arm_config: PiperLeaderArmConfig
    right_arm_config: PiperLeaderArmConfig

    @property
    def type(self) -> str:
        return self.get_choice_name(self.__class__)
