"""텔레오퍼레이션 세션 — **지금은 상태만** (feature/teleoperation.md 1단계).

움직이는 코드는 아직 없다. 그런데 배타 표와 E-stop 대상에는 **먼저** 올라야 한다:
팔을 움직이는 기능을 넣고 나서 멈추는 길을 만들면, 그 사이에 멈출 수 없는
기능이 존재하는 구간이 생긴다.

`is_running` 이 늘 False 인 채로 두지 않고 실제 세션 상태를 갖는 이유도 같다 —
표가 거짓말을 하면(막는다고 적어놓고 안 막으면) 그 표를 아무도 안 믿는다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TeleopSession:
    """지금 어느 팔을 조종 중인가. 한 번에 하나다."""

    iface: str | None = None
    mode: str = ""            # leader | joint | endpoint
    started: float = 0.0
    _log: list = field(default_factory=list, repr=False)

    @property
    def is_running(self) -> bool:
        return self.iface is not None

    def start(self, iface: str, mode: str) -> tuple[bool, str]:
        if self.is_running:
            return False, f"이미 {self.iface} 를 조종 중입니다"
        self.iface, self.mode, self.started = iface, mode, time.time()
        logger.info("텔레오퍼레이션 시작: %s (%s)", iface, mode)
        return True, "OK"

    def stop(self) -> None:
        if self.iface:
            logger.info("텔레오퍼레이션 정지: %s", self.iface)
        self.iface, self.mode, self.started = None, "", 0.0

    async def kill(self) -> None:
        """E-stop 경로. **토크는 robotd 가 끊는다** — 여기서는 세션만 닫는다.

        토크 차단을 게이트웨이가 하면 게이트웨이가 멈춰 있을 때 안 된다.
        그래서 팔을 쥔 쪽(robotd)이 알림을 직접 듣는다.
        """
        self.stop()

    def to_dict(self) -> dict:
        return {"running": self.is_running, "iface": self.iface, "mode": self.mode,
                "started": self.started or None}


teleop_session = TeleopSession()
