from dataclasses import dataclass

from lerobot.teleoperators.config import TeleoperatorConfig


# 평면 dataclass — 양팔 설정이 중첩하는 쪽 (config_piper.py 의 주석 참고)
@dataclass(kw_only=True)
class PiperLeaderArmConfig:
    # Port to connect to the arm
    port: str

    # Sets the arm in torque mode with the gripper motor set to this value.
    gripper_open_pos: float = 50.0


@TeleoperatorConfig.register_subclass("piper_leader")
@dataclass(kw_only=True)
class PiperLeaderConfig(TeleoperatorConfig, PiperLeaderArmConfig):
    pass
