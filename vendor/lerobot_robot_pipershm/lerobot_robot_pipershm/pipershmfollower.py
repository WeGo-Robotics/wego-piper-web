"""`PiperFollower` 의 버스만 바꿔 끼운 프록시 로봇.

**`__init__` 말고는 아무것도 오버라이드하지 않는다.** 그게 요점이다 —
`get_observation`/`send_action`/`observation_features`/`action_features` 와
카메라 병렬 읽기, `ensure_safe_goal_position` 클램핑이 전부 그대로 재사용된다.
한 줄이라도 베껴오면 그 순간부터 정책·데이터셋 계약이 갈릴 수 있다.
"""

import logging

from lerobot.cameras import Camera
from lerobot.cameras.utils import make_cameras_from_configs
from lerobot.robots import Robot
from lerobot_robot_piper.piper_follower import PiperFollower

from .config_pipershmfollower import PiperShmFollowerConfig
from .motor_specs import CALIBRATION, MOTORS
from .shm_motors_bus import PiperShmMotorsBus

logger = logging.getLogger(__name__)


class PiperShmFollower(PiperFollower):

    config_class = PiperShmFollowerConfig
    name = "piper_follower_shm"

    def __init__(self, config: PiperShmFollowerConfig):
        # ⚠ `PiperFollower.__init__` 을 **부르지 않는다.** 그게 `PiperMotorsBus` 를
        # 만들면서 `C_PiperInterface_V2(port)` 로 CAN 을 연다 — 프록시가
        # CAN 을 여는 순간 robotd 와 같은 버스를 두 프로세스가 만지게 된다.
        # 조부모(`Robot`)를 직접 부르고 버스만 바꿔 끼운다.
        Robot.__init__(self, config)
        self.config = config
        self.id = config.id
        self.port = config.port
        self.cameras: dict[str, Camera] = {}
        # 모터·캘리브레이션은 직접 드라이버와 **같은 값이어야 한다** —
        # 갈리면 `observation_features` 가 갈리고 정책이 조용히 틀린 관측을 받는다.
        # 상류가 별도 저장소라 복사본을 두고 테스트로 대조한다 (`motor_specs.py`).
        self.bus = PiperShmMotorsBus(
            id=config.id, port=config.port,
            motors=dict(MOTORS), calibration=dict(CALIBRATION),
            deadman_ms=config.deadman_ms,
        )
        self.cameras = make_cameras_from_configs(config.cameras)
        self._camera_executor = None
        if self.cameras:
            from concurrent.futures import ThreadPoolExecutor

            self._camera_executor = ThreadPoolExecutor(
                max_workers=len(self.cameras), thread_name_prefix="cam_read",
            )
