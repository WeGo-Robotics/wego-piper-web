"""LeRobot 카메라 플러그인 — `/dev/shm` 세그먼트에서 프레임을 받는다.

`register_third_party_plugins()` 가 `lerobot_camera_*` 배포판을 자동 import 하므로
설치만 하면 `type: "shm"` 이 생긴다 (LeRobot 수정 0).
"""

from .config_pipershmcamera import PiperShmCameraConfig
from .pipershmcamera import PiperShmCamera

__all__ = ["PiperShmCamera", "PiperShmCameraConfig"]
