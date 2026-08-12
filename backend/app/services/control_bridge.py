"""녹화 에피소드 제어 채널 (백엔드 → wrapper).

헤드리스 환경에서는 pynput 키 주입이 불가능하고(X 없음), LeRobot 도 키보드
리스너를 끄기 때문에 건너뛰기/재녹화/정지 버튼이 동작하지 않는다. 대신 wrapper
가 LeRobot 의 events dict 를 직접 set 하도록, 여기서 명령을 버스에 올린다.

명령: `right`(건너뛰기/exit_early), `left`(재녹화), `escape`(정지) —
이름은 [piper_bus.contract](../../../../bus/piper_bus/contract.py) 가 갖는다.

ZMQ PUSH(`tcp://127.0.0.1:5557`) 를 Redis 큐로 교체했다 (refactor/daemon-split.md 3단계).

## ⚠ 세션 격리가 이 파일의 핵심이다

ZMQ 시절에는 **소켓이 없으면 큐도 없어서** 세션 간에 명령이 샐 수 없었다
("비녹화 시에는 소켓이 없어 send() 가 no-op").
**Redis 리스트는 소켓과 무관하게 살아남는다.** 그래서 두 가지를 유지한다:

1. `_active` 플래그 — 녹화 중이 아니면 `send()` 가 no-op (ZMQ 시절과 같은 동작)
2. `start()`/`stop()` 에서 큐를 **비운다** — 남은 명령이 다음 녹화 첫 에피소드를
   건너뛰거나 지워버리는 사고를 막는다
"""

import logging

from piper_bus import contract as C
from piper_bus.client import Bus

logger = logging.getLogger(__name__)


class ControlBridge:
    def __init__(self, bus: Bus | None = None) -> None:
        self._bus = bus
        self._explicit = bus is not None
        self._active = False

    def _connect(self) -> Bus | None:
        if self._bus is None and not self._explicit:
            try:
                self._bus = Bus()
            except Exception as exc:
                logger.error("ControlBridge 버스 연결 실패: %s", exc)
                return None
        return self._bus

    def start(self) -> None:
        bus = self._connect()
        if bus is None:
            return
        try:
            dropped = bus.clear_control()
        except Exception as exc:
            logger.error("ControlBridge 시작 실패: %s", exc)
            return
        if dropped:
            logger.warning("이전 세션의 제어 명령 %d개를 버렸습니다", dropped)
        self._active = True
        logger.info("ControlBridge started (Redis)")

    def send(self, command: str) -> bool:
        if not self._active:
            logger.warning("ControlBridge not active; dropping command %r", command)
            return False
        if command not in C.CONTROL_COMMANDS:
            logger.warning("알 수 없는 제어 명령: %r", command)
            return False
        bus = self._connect()
        if bus is None:
            return False
        try:
            bus.push_control(command)
            return True
        except Exception as exc:
            logger.warning("ControlBridge send %r failed: %s", command, exc)
            return False

    def stop(self) -> None:
        self._active = False
        bus = self._bus
        if bus is not None:
            try:
                bus.clear_control()
            except Exception as exc:
                logger.warning("ControlBridge 정리 실패: %s", exc)
        logger.info("ControlBridge stopped")


control_bridge = ControlBridge()
