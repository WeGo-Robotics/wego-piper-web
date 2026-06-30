"""녹화 중 카메라 프레임 미리보기 브리지.

녹화 wrapper(LeRobot in-process)가 log_rerun_data 탭에서 JPEG 프레임을
ZMQ PUSH 로 보내면, 여기서 PULL 로 받아 카메라별 최신 프레임을 캐시한다.
기존 /api/cameras/{id}/preview 와 같은 단일-JPEG 폴링 UI 로 노출하기 위함.

녹화 프로세스와 분리된 협력 모듈: 프레임이 끊겨도(브리지 미실행/지연) 녹화는
독립적으로 진행되고, 미리보기만 비는 것으로 격리된다.
"""

import logging
import threading
import time

import zmq

from app.core.config import settings

logger = logging.getLogger(__name__)

# 이 시간(초)보다 오래된 프레임은 stale 로 간주 (names() 에서 제외)
_FRESH_SECONDS = 3.0


class PreviewBridge:
    def __init__(self, address: str | None = None) -> None:
        self._address = address or settings.preview_zmq_address
        self._ctx: zmq.Context | None = None
        self._sock: zmq.Socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._frames: dict[str, bytes] = {}
        self._ts: dict[str, float] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return
        try:
            self._ctx = zmq.Context.instance()
            self._sock = self._ctx.socket(zmq.PULL)
            self._sock.setsockopt(zmq.RCVHWM, 4)
            self._sock.setsockopt(zmq.RCVTIMEO, 500)  # 루프가 _running 을 주기적으로 확인
            self._sock.setsockopt(zmq.LINGER, 0)
            self._sock.bind(self._address)
        except Exception as exc:
            logger.error("PreviewBridge bind failed (%s): %s", self._address, exc)
            self._cleanup_socket()
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("PreviewBridge listening on %s", self._address)

    def _loop(self) -> None:
        assert self._sock is not None
        while self._running:
            try:
                name_b, jpeg = self._sock.recv_multipart()
            except zmq.Again:
                continue
            except Exception as exc:
                if self._running:
                    logger.warning("PreviewBridge recv error: %s", exc)
                continue
            name = name_b.decode("utf-8", errors="replace")
            now = time.monotonic()
            with self._lock:
                self._frames[name] = jpeg
                self._ts[name] = now

    def get(self, name: str) -> bytes | None:
        with self._lock:
            return self._frames.get(name)

    def names(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            return [n for n, t in self._ts.items() if now - t <= _FRESH_SECONDS]

    def stop(self) -> None:
        self._running = False
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None
        self._cleanup_socket()
        with self._lock:
            self._frames.clear()
            self._ts.clear()
        logger.info("PreviewBridge stopped")

    def _cleanup_socket(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close(linger=0)
            except Exception:
                pass
            self._sock = None
        # Context.instance() 는 공용 싱글톤이므로 term 하지 않는다.
        self._ctx = None


preview_bridge = PreviewBridge()
