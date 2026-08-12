"""녹화 중 카메라 프레임 미리보기 브리지.

녹화 wrapper(LeRobot in-process)가 log_rerun_data 탭에서 JPEG 프레임을 버스에 올리면,
여기서 읽어 기존 `/api/cameras/{id}/preview` 와 같은 단일-JPEG 폴링 UI 로 노출한다.

ZMQ PULL(`tcp://127.0.0.1:5556`) 을 Redis 로 교체했다 (refactor/daemon-split.md 3단계).

## 수신 스레드가 없어졌다

ZMQ 시절에는 소켓을 bind 하고 **백그라운드 스레드가 프레임을 받아 dict 에 쌓았다.**
Redis 가 그 dict 역할을 하므로 스레드·락·`_ts` 추적이 통째로 사라진다.
`_FRESH_SECONDS` 판정도 키 TTL 로 대체됐다 — stale 프레임은 Redis 가 만료시킨다.

녹화 프로세스와 분리된 협력 모듈이라는 성격은 그대로다: 프레임이 끊겨도 녹화는
독립적으로 진행되고, 미리보기만 비는 것으로 격리된다.
"""

import logging

from piper_bus.client import Bus

logger = logging.getLogger(__name__)


class PreviewBridge:
    def __init__(self, bus: Bus | None = None) -> None:
        self._bus = bus
        self._explicit = bus is not None

    def _connect(self) -> Bus | None:
        if self._bus is None and not self._explicit:
            try:
                self._bus = Bus()
            except Exception as exc:
                logger.error("PreviewBridge 버스 연결 실패: %s", exc)
                return None
        return self._bus

    def start(self) -> None:
        """녹화 시작. **이전 세션의 프레임을 지운다** — 안 지우면 TTL 이 만료될 때까지
        지난 녹화의 마지막 화면이 새 녹화의 미리보기로 보인다."""
        bus = self._connect()
        if bus is None:
            return
        try:
            bus.clear_previews()
        except Exception as exc:
            logger.warning("PreviewBridge 초기화 실패: %s", exc)
            return
        logger.info("PreviewBridge started (Redis)")

    def get(self, name: str) -> bytes | None:
        bus = self._connect()
        if bus is None:
            return None
        try:
            return bus.get_preview(name)
        except Exception as exc:
            logger.warning("PreviewBridge get(%s) 실패: %s", name, exc)
            return None

    def names(self) -> list[str]:
        """살아 있는 프리뷰 카메라. TTL 이 지난 것은 Redis 가 이미 지웠다."""
        bus = self._connect()
        if bus is None:
            return []
        try:
            return bus.preview_names()
        except Exception as exc:
            logger.warning("PreviewBridge names() 실패: %s", exc)
            return []

    def stop(self) -> None:
        bus = self._bus
        if bus is None:
            return
        try:
            bus.clear_previews()
        except Exception as exc:
            logger.warning("PreviewBridge 정리 실패: %s", exc)
        logger.info("PreviewBridge stopped")


preview_bridge = PreviewBridge()
