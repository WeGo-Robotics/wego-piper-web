"""양팔 Piper 팔로워 — `PiperFollower` 둘의 합성 (상류 `BiSOFollower` 관용구).

관측·액션 키는 `left_`/`right_` 접두사로 병합한다. 이 접두사 규약은
grpc_wrapper 의 즉석 조립이 이미 쓰던 것과 동일하므로, 기존 양팔 gRPC
산출물·데이터셋과 키 이름이 그대로 호환된다.

액션 전송·파킹은 두 팔을 **병렬**로 보낸다 — 순차로 보내면 오른팔이
왼팔보다 한 전송 지연만큼 늦게 움직여 협조 동작이 비틀린다.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from functools import cached_property
from typing import Any

from lerobot.robots import Robot

from .config_bi_piper import BiPiperFollowerConfig
from .config_piper import PiperFollowerConfig
from .piper_follower import PiperFollower

logger = logging.getLogger(__name__)


class BiPiperFollower(Robot):

    config_class = BiPiperFollowerConfig
    name = "bi_piper_follower"

    # 서브클래스(shm)가 이 둘만 갈아끼운다
    arm_class = PiperFollower
    arm_config_class = PiperFollowerConfig

    def __init__(self, config: BiPiperFollowerConfig):
        super().__init__(config)
        self.config = config
        self.left_arm = self._make_arm("left", config.left_arm_config)
        self.right_arm = self._make_arm("right", config.right_arm_config)
        # `robot.cameras` 를 기대하는 코드와의 호환용 (상류와 동일)
        self.cameras = {**self.left_arm.cameras, **self.right_arm.cameras}
        self._pair = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bi_arm")

    def _make_arm(self, side: str, arm_cfg):
        cfg = self.arm_config_class(
            **{
                **{f.name: getattr(arm_cfg, f.name) for f in arm_cfg.__dataclass_fields__.values()},
                "id": f"{self.config.id}_{side}" if self.config.id else None,
                "calibration_dir": self.config.calibration_dir,
            }
        )
        return self.arm_class(cfg)

    def __str__(self) -> str:
        return f"{self.id} {self.__class__.__name__}"

    @property
    def _motors_ft(self) -> dict[str, type]:
        return {
            **{f"left_{k}": v for k, v in self.left_arm._motors_ft.items()},
            **{f"right_{k}": v for k, v in self.right_arm._motors_ft.items()},
        }

    @property
    def _cameras_ft(self) -> dict[str, tuple]:
        return {
            **{f"left_{k}": v for k, v in self.left_arm._cameras_ft.items()},
            **{f"right_{k}": v for k, v in self.right_arm._cameras_ft.items()},
        }

    @cached_property
    def observation_features(self) -> dict:
        return {**self._motors_ft, **self._cameras_ft}

    @cached_property
    def action_features(self) -> dict:
        return self._motors_ft

    @property
    def is_connected(self) -> bool:
        return self.left_arm.is_connected and self.right_arm.is_connected

    def connect(self, calibrate: bool = True) -> None:
        self.left_arm.connect(calibrate)
        self.right_arm.connect(calibrate)

    @property
    def is_calibrated(self) -> bool:
        return self.left_arm.is_calibrated and self.right_arm.is_calibrated

    def calibrate(self) -> None:
        self.left_arm.calibrate()
        self.right_arm.calibrate()

    def configure(self) -> None:
        self.left_arm.configure()
        self.right_arm.configure()

    def setup_motors(self) -> None:
        self.left_arm.setup_motors()
        self.right_arm.setup_motors()

    def get_observation(self) -> dict[str, Any]:
        obs: dict[str, Any] = {}
        obs.update({f"left_{k}": v for k, v in self.left_arm.get_observation().items()})
        obs.update({f"right_{k}": v for k, v in self.right_arm.get_observation().items()})
        return obs

    def send_action(self, action: dict[str, Any]) -> dict[str, Any]:
        left = {k.removeprefix("left_"): v for k, v in action.items() if k.startswith("left_")}
        right = {k.removeprefix("right_"): v for k, v in action.items() if k.startswith("right_")}
        f_left = self._pair.submit(self.left_arm.send_action, left)
        f_right = self._pair.submit(self.right_arm.send_action, right)
        return {
            **{f"left_{k}": v for k, v in f_left.result().items()},
            **{f"right_{k}": v for k, v in f_right.result().items()},
        }

    def parking(self) -> None:
        f_left = self._pair.submit(self.left_arm.parking)
        f_right = self._pair.submit(self.right_arm.parking)
        f_left.result()
        f_right.result()

    def disconnect(self, disable_torque: bool = False) -> None:
        self._pair.shutdown(wait=False)
        self.left_arm.disconnect(disable_torque)
        self.right_arm.disconnect(disable_torque)
