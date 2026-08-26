"""`type: "shm"` 카메라 설정.

LeRobot 수정은 0이다 — `CameraConfig` 가 `draccus.ChoiceRegistry` 라
`register_subclass` 한 줄로 새 타입이 생기고,
`make_cameras_from_configs` 의 마지막 `else` 가 `make_device_from_device_class(cfg)` 로
빠지면서 **클래스명에서 `Config` 를 뗀 구현 클래스**를 같은 패키지에서 찾는다.

    PiperShmCameraConfig  →  PiperShmCamera

같은 메커니즘을 `vendor/lerobot_robot_piper/` 가 이미 쓰고 있다 (선례).
"""

from dataclasses import dataclass

from lerobot.cameras.configs import CameraConfig, ColorMode


@CameraConfig.register_subclass("shm")
@dataclass
class PiperShmCameraConfig(CameraConfig):
    """`/dev/shm` 세그먼트에서 프레임을 받는다.

    장치를 직접 열지 않으므로 이 프로세스에는 **USB·RealSense 권한이 필요 없다.**
    D405 의 "color-only 는 0fps" 같은 하드웨어 특수사정도 발행자(camerad) 안에 갇힌다.
    """

    # 세그먼트 이름 (`/dev/shm/piper.cam.<segment>`).
    # 비우면 카메라 키를 그대로 쓴다 — `{"top": {"type": "shm"}}` 로 충분하게.
    segment: str = ""

    # 첫 프레임을 기다리는 상한. 발행자가 아직 안 떴을 수 있다.
    warmup_s: float = 5.0

    # ⚠ **세그먼트는 BGR 이고 LeRobot 은 RGB 를 기대한다.**
    #
    # 이 필드가 없던 동안 플러그인이 세그먼트 프레임을 그대로 돌려줬고,
    # LeRobot 은 그걸 RGB 로 알고 데이터셋에 구웠다 — 녹화된 에피소드 전체가
    # R 과 B 가 뒤바뀐 채 저장됐다(주황 상자가 파랗게). `OpenCVCamera` 는
    # 같은 상황을 `color_mode` 로 다룬다. 같은 이름, 같은 기본값을 쓴다.
    color_mode: ColorMode = ColorMode.RGB
