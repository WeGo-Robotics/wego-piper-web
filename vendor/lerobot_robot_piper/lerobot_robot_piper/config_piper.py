from dataclasses import dataclass, field

from lerobot.cameras import CameraConfig
from lerobot.robots.config import RobotConfig


# ⚠ 평면 dataclass 로 남겨야 한다 (RobotConfig 상속 금지) — 상류 `SOFollowerConfig` 와
# 같은 이유다. draccus 는 ChoiceType(=RobotConfig 계열) 타입의 필드를 만나면 전체
# 로봇 레지스트리를 다시 펼치는데, 양팔 설정이 팔 설정을 중첩하므로 등록형을
# 중첩하면 인자 등록이 무한 재귀한다 (bi → arm → 레지스트리 → bi → …).
@dataclass(kw_only=True)
class PiperArmConfig:
    # Port to connect to the arm
    port: str

    disable_torque_on_disconnect: bool = True

    cameras: dict[str, CameraConfig] = field(default_factory=dict)

    # `max_relative_target` limits the magnitude of the relative positional target vector for safety purposes.
    # Set this to a positive scalar to have the same value for all motors, or a dictionary that maps motor
    # names to the max_relative_target value for that motor.
    max_relative_target: float | dict[str, float] | None = None


@RobotConfig.register_subclass("piper_follower")
@dataclass(kw_only=True)
class PiperFollowerConfig(RobotConfig, PiperArmConfig):

    @property
    def type(self) -> str:
        return self.get_choice_name(self.__class__)
