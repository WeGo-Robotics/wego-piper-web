"""양팔 Piper 리더 — `PiperLeader` 둘의 합성 (상류 `BiSOLeader` 관용구).

액션 키는 `left_`/`right_` 접두사로 병합 — `BiPiperFollower.send_action` 이
같은 접두사로 도로 갈라 보낸다.
"""

import logging
from functools import cached_property
from typing import Any

from lerobot.teleoperators.teleoperator import Teleoperator

from .config_bi_piper import BiPiperLeaderConfig
from .config_piper_leader import PiperLeaderConfig
from .piper_leader import PiperLeader

logger = logging.getLogger(__name__)


class BiPiperLeader(Teleoperator):

    config_class = BiPiperLeaderConfig
    name = "bi_piper_leader"

    # 서브클래스(shm)가 이 둘만 갈아끼운다
    arm_class = PiperLeader
    arm_config_class = PiperLeaderConfig

    def __init__(self, config: BiPiperLeaderConfig):
        super().__init__(config)
        self.config = config
        self.left_arm = self._make_arm("left", config.left_arm_config)
        self.right_arm = self._make_arm("right", config.right_arm_config)

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

    @cached_property
    def action_features(self) -> dict[str, type]:
        return {
            **{f"left_{k}": v for k, v in self.left_arm.action_features.items()},
            **{f"right_{k}": v for k, v in self.right_arm.action_features.items()},
        }

    @cached_property
    def feedback_features(self) -> dict[str, type]:
        return {}

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

    def get_action(self) -> dict[str, Any]:
        return {
            **{f"left_{k}": v for k, v in self.left_arm.get_action().items()},
            **{f"right_{k}": v for k, v in self.right_arm.get_action().items()},
        }

    def send_feedback(self, feedback: dict[str, float]) -> None:
        raise NotImplementedError

    def disconnect(self) -> None:
        self.left_arm.disconnect()
        self.right_arm.disconnect()
