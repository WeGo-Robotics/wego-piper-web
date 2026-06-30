"""녹화 에피소드 제어 채널 (백엔드 → wrapper).

헤드리스 환경에서는 pynput 키 주입이 불가능하고(X 없음), LeRobot 도 키보드
리스너를 끄기 때문에 건너뛰기/재녹화/정지 버튼이 동작하지 않는다. 대신 wrapper
가 LeRobot 의 events dict 를 직접 set 하도록, 여기서 명령 문자열을 PUSH 한다.

명령: "right"(건너뛰기/exit_early), "left"(재녹화), "escape"(정지).

녹화 세션 동안만 소켓을 유지한다(start→stop). 비녹화 시에는 소켓이 없어
send() 가 no-op 이므로, 세션 간에 명령이 큐에 남아 다음 녹화로 새는 일이 없다.
wrapper(PULL)가 bind, 백엔드(PUSH)가 connect 한다.
"""

import logging

import zmq

from app.core.config import settings

logger = logging.getLogger(__name__)


class ControlBridge:
    def __init__(self, address: str | None = None) -> None:
        self._address = address or settings.control_zmq_address
        self._sock: zmq.Socket | None = None

    def start(self) -> None:
        if self._sock is not None:
            return
        try:
            ctx = zmq.Context.instance()
            sock = ctx.socket(zmq.PUSH)
            sock.setsockopt(zmq.SNDHWM, 8)
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(self._address)
            self._sock = sock
            logger.info("ControlBridge connected to %s", self._address)
        except Exception as exc:
            logger.error("ControlBridge start failed (%s): %s", self._address, exc)
            self._sock = None

    def send(self, command: str) -> bool:
        if self._sock is None:
            logger.warning("ControlBridge not active; dropping command %r", command)
            return False
        try:
            self._sock.send_string(command, flags=zmq.NOBLOCK)
            return True
        except Exception as exc:
            logger.warning("ControlBridge send %r failed: %s", command, exc)
            return False

    def stop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close(linger=0)
            except Exception:
                pass
            self._sock = None
        logger.info("ControlBridge stopped")


control_bridge = ControlBridge()
