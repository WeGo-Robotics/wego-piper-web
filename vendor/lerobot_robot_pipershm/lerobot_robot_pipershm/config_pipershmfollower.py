"""`robot.type = "piper_follower_shm"` 등록.

카메라 플러그인(`lerobot_camera_pipershm`)과 **같은 메커니즘**이다 —
`register_third_party_plugins()` 가 `lerobot_robot_*` 배포판을 자동 import 하고,
`make_robot_from_config` 의 마지막 `else` 가 `make_device_from_device_class(config)`
로 빠진다. **LeRobot 수정 0.**

`PiperFollowerConfig` 를 상속해 필드를 물려받는다 — 필드가 갈리면 같은 로봇에
대해 두 벌의 설정이 생기고, 어느 쪽이 진짜인지 알 수 없게 된다.
"""

from dataclasses import dataclass

from lerobot.robots.config import RobotConfig
from lerobot_robot_piper.config_piper import PiperFollowerConfig

from .shm_motors_bus import DEFAULT_DEADMAN_MS


@RobotConfig.register_subclass("piper_follower_shm")
@dataclass(kw_only=True)
class PiperShmFollowerConfig(PiperFollowerConfig):
    """`port` 는 여기서 **CAN 인터페이스 이름**이다 (`can0`).

    프록시는 CAN 을 열지 않지만 이름은 그대로 쓴다 — 세그먼트 이름이 인터페이스에서
    나오므로(`piper.arm.can0.state`), robotd 와 소비자가 같은 이름으로 만난다.
    """

    # 소비자가 선언하는 자기 제어 주기의 상한. robotd 는 이 시간 동안 명령이
    # 안 오면 팔을 세운다. **0 이면 데드맨을 끈다** — 텔레오퍼레이션처럼 사람이
    # 보고 있는 경우를 위해 남겨두지만, 자동 실행에서는 반드시 켠다.
    deadman_ms: int = DEFAULT_DEADMAN_MS

    @property
    def type(self) -> str:
        return self.get_choice_name(self.__class__)
