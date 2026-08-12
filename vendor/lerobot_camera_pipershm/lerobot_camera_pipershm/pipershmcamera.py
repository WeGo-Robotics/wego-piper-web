"""`/dev/shm` 세그먼트를 읽는 LeRobot 카메라.

## 이 프로세스는 장치를 열지 않는다

기존에는 녹화·추론 subprocess 가 v4l2/RealSense 를 **직접** 열었다. 그래서
웹이 잡고 있던 카메라를 매번 해제해야 했고(`_release_all_cameras`), 컨테이너는
`privileged` + `/dev` 마운트가 필요했다.

여기서는 발행자(camerad)가 장치를 독점하고 이쪽은 픽셀만 읽는다:

- 장치 권한 불필요 → 컨테이너에서 `privileged` 제거 가능 (`ipc: host` 만)
- 해제 춤 소멸 — 소유자가 하나뿐이라 뺏고 뺏길 일이 없다
- D405 의 "color-only 는 0fps" 같은 특수사정이 발행자 안에 갇힌다
- **JPEG 이중압축이 없다** — raw 가 그대로 데이터셋에 들어간다
"""

import logging

import numpy as np
from lerobot.cameras.camera import Camera
from lerobot.utils.errors import DeviceAlreadyConnectedError, DeviceNotConnectedError
from piper_shm import SegmentError, Subscriber, list_segments

from .config_pipershmcamera import PiperShmCameraConfig

logger = logging.getLogger(__name__)


class PiperShmCamera(Camera):
    def __init__(self, config: PiperShmCameraConfig):
        super().__init__(config)
        self.config = config
        self._segment = config.segment
        self._sub: Subscriber | None = None

    # 세그먼트 이름은 설정에 없으면 카메라 키를 쓴다.
    # LeRobot 이 키를 `Camera` 에 직접 주지 않으므로 연결 시점에 받아둔다.
    def set_default_segment(self, name: str) -> None:
        if not self._segment:
            self._segment = name

    @property
    def is_connected(self) -> bool:
        return self._sub is not None

    @staticmethod
    def find_cameras() -> list[dict]:
        """살아 있는 세그먼트 목록.

        **세그먼트의 존재 자체가 lease 다** — 발행자가 그 카메라를 잡고 있다는 뜻이다.
        """
        return [{"type": "shm", "id": name, "segment": name} for name in list_segments()]

    def connect(self, warmup: bool = True) -> None:
        if self.is_connected:
            raise DeviceAlreadyConnectedError(f"{self} 는 이미 연결됨")
        if not self._segment:
            raise DeviceNotConnectedError(
                "세그먼트 이름이 없습니다 — 설정에 `segment` 를 주거나 카메라 키를 쓰세요"
            )
        try:
            self._sub = Subscriber(self._segment)
        except SegmentError as e:
            # 발행자가 안 떠 있으면 여기서 **깨끗하게** 죽는다 — 좀비로 남지 않는다
            raise DeviceNotConnectedError(f"{self}: {e}") from e

        h, w, _ = self._sub.shape
        if self.width and self.height and (w, h) != (self.width, self.height):
            logger.warning(
                "%s: 세그먼트가 %dx%d 인데 설정은 %dx%d — 세그먼트 값을 따릅니다",
                self, w, h, self.width, self.height,
            )
        self.width, self.height = w, h

        if warmup and self._sub.read_new(timeout_s=self.config.warmup_s) is None:
            self.disconnect()
            raise DeviceNotConnectedError(
                f"{self}: {self.config.warmup_s}초 안에 프레임이 오지 않았습니다 "
                "(발행자가 캡처 중인가요?)"
            )
        logger.info("%s connected (%dx%d)", self, self.width, self.height)

    def read(self, color_mode=None) -> np.ndarray:
        """지금 있는 최신 프레임. 새 프레임을 기다리지 않는다."""
        if self._sub is None:
            raise DeviceNotConnectedError(f"{self} 가 연결되지 않음")
        got = self._sub.read()
        if got is None:
            raise DeviceNotConnectedError(f"{self}: 프레임을 읽지 못했습니다")
        return got[0]

    def async_read(self, timeout_ms: float = 200) -> np.ndarray:
        """**새** 프레임을 기다렸다 반환.

        백그라운드 스레드가 필요 없다 — 발행자가 이미 별도 프로세스라
        여기서는 `write_seq` 만 지켜보면 된다.
        """
        if self._sub is None:
            raise DeviceNotConnectedError(f"{self} 가 연결되지 않음")
        got = self._sub.read_new(timeout_s=timeout_ms / 1000.0)
        if got is None:
            raise TimeoutError(f"{self}: {timeout_ms}ms 안에 새 프레임이 없습니다")
        return got[0]

    def disconnect(self) -> None:
        if self._sub is not None:
            if self._sub.retries:
                # 비정상적으로 높으면 라이터가 너무 빠르거나 슬롯이 부족하다는 신호
                logger.info("%s: seqlock 재시도 %d회", self, self._sub.retries)
            self._sub.close()
            self._sub = None

    def __str__(self) -> str:
        return f"PiperShmCamera({self._segment or '?'})"
