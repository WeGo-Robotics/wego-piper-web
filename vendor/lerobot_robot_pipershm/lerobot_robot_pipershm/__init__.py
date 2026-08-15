"""`piper-robotd` 와 `/dev/shm` 으로 통신하는 LeRobot 로봇 플러그인.

`register_third_party_plugins()` 가 이 배포판을 import 하면 config 등록이 일어나
`--robot.type=piper_follower_shm` / `--teleop.type=piper_leader_shm` 이 쓸 수
있게 된다. **LeRobot 수정 0.**
"""

from .config_pipershmfollower import PiperShmFollowerConfig
from .config_pipershmleader import PiperShmLeaderConfig
from .pipershmfollower import PiperShmFollower
from .pipershmleader import PiperShmLeader
from .bipipershm import (
    BiPiperShmFollower,
    BiPiperShmFollowerConfig,
    BiPiperShmLeader,
    BiPiperShmLeaderConfig,
)
from .shm_motors_bus import PiperShmMotorsBus

__all__ = [
    "PiperShmFollower", "PiperShmFollowerConfig",
    "PiperShmLeader", "PiperShmLeaderConfig",
    "BiPiperShmFollower", "BiPiperShmFollowerConfig",
    "BiPiperShmLeader", "BiPiperShmLeaderConfig",
    "PiperShmMotorsBus",
]
