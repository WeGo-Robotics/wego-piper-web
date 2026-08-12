"""등록된 카메라 프레임을 `/dev/shm` 세그먼트로 흘린다 (발행측).

## 왜 지금 게이트웨이가 이 일을 하나

`camerad` 를 분리하기 **전에 전송 계층만** 검증하기 위해서다
(refactor/camera-transport.md 착수 순서 2단계 — *"데몬을 쪼개기 전에 전송이 되는지부터"*).

기존 캡처 경로에 **얹기만 한다** — `_last_frame` 도, 프리뷰도, 녹화도 그대로다.
발행이 실패해도 기존 동작은 아무 영향이 없다. camerad 로 옮길 때는
이 모듈의 사용처만 바뀌고 세그먼트 포맷은 그대로다.

## 이름 = LeRobot 카메라 키

세그먼트 이름은 **정책이 보는 키**(`top`, `wrist`)여야 한다.
카메라 별칭(`탑뷰`)이나 장치 id(`rs:...:color`)가 아니다 —
소비자는 `{"top": {"type": "shm"}}` 로 여는데, 그 키가 곧 세그먼트 이름이다.
"""

import logging
import threading

import numpy as np
from piper_shm import Publisher, list_segments, unlink

logger = logging.getLogger(__name__)


class ShmPublisher:
    """카메라 키 → 세그먼트. 해상도가 바뀌면 세그먼트를 다시 만든다."""

    def __init__(self) -> None:
        self._pubs: dict[str, Publisher] = {}
        self._lock = threading.Lock()

    def start(self, mapping: dict[str, str]) -> list[str]:
        """`{카메라키: 장치id}` 로 세그먼트를 연다. 연 키 목록을 돌려준다.

        해상도는 첫 프레임에서 정해지므로 여기서는 자리만 잡지 않는다 —
        `publish()` 가 필요할 때 만든다.
        """
        with self._lock:
            self._stop_all_locked()
        logger.info("shm 발행 준비: %s", list(mapping))
        return list(mapping)

    def publish(self, key: str, frame: np.ndarray) -> bool:
        """프레임 하나 발행. **실패해도 예외를 올리지 않는다.**

        캡처 루프에서 불리므로, 여기서 던지면 프리뷰·녹화까지 같이 멈춘다.
        """
        if frame is None or frame.ndim != 3:
            return False
        h, w, ch = frame.shape
        try:
            with self._lock:
                pub = self._pubs.get(key)
                if pub is None or pub.layout.height != h or pub.layout.width != w:
                    if pub is not None:
                        # 해상도가 바뀌었다 — 소비자가 옛 크기로 읽으면 쓰레기가 된다
                        logger.info("세그먼트 재생성 %s: %dx%d", key, w, h)
                        pub.close()
                    pub = Publisher(key, width=w, height=h, channels=ch)
                    self._pubs[key] = pub
                pub.publish(np.ascontiguousarray(frame))
            return True
        except Exception as exc:
            logger.warning("shm 발행 실패 (%s): %s", key, exc)
            return False

    def stop(self, key: str) -> None:
        with self._lock:
            pub = self._pubs.pop(key, None)
        if pub is not None:
            pub.close()

    def stop_all(self) -> None:
        with self._lock:
            self._stop_all_locked()

    def _stop_all_locked(self) -> None:
        for key, pub in list(self._pubs.items()):
            try:
                pub.close()
            except Exception as exc:
                logger.warning("세그먼트 정리 실패 (%s): %s", key, exc)
        self._pubs.clear()

    def active(self) -> list[str]:
        with self._lock:
            return sorted(self._pubs)


def sweep_stale_segments(keep: set[str] | None = None) -> list[str]:
    """남은 세그먼트를 치운다.

    ⚠ 프로세스가 죽으면 `/dev/shm` 파일은 **그대로 남는다.** 치우지 않으면
    다음 실행에서 소비자가 옛 세그먼트를 열어 **멈춘 화면**을 보게 된다 —
    발행자가 없으니 프레임은 안 오는데 파일은 있어서 연결은 성공한다.
    기동 시 한 번 쓸어낸다.
    """
    keep = keep or set()
    removed = [name for name in list_segments() if name not in keep and unlink(name)]
    if removed:
        logger.info("남은 shm 세그먼트 %d개 정리: %s", len(removed), removed)
    return removed


shm_publisher = ShmPublisher()
