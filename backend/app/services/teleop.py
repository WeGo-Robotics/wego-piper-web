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


# ── 팔의 명령 경로를 넘겨받기 ──
#
# 조그와 리더 릴레이가 **같은 일**을 한다: 팔로워의 action 세그먼트를 연다.
# 위험한 부분이 거기라 한 곳에 둔다.


class ArmBusyError(RuntimeError):
    """명령 경로를 못 잡는 이유. 호출부가 그대로 사용자에게 보여준다."""


def open_action_writer(iface: str, deadman_ms: int):
    """팔의 명령 경로를 연다. 이미 누가 쥐고 있으면 **거절한다.**

    ⚠ `ActionWriter` 는 `O_CREAT` 라 기존 세그먼트를 **조용히 덮는다.** 추론
    프록시가 조종 중인데 그 위에 열면 팔의 명령 경로를 가로채는 셈이다.
    "세그먼트 존재 = 조종 중"은 관례지 강제가 아니므로 여기서 확인한다.
    """
    from piper_shm import arm as shm_arm

    name = shm_arm.segment_name(iface, shm_arm.KIND_ACTION)
    if name in set(shm_arm.list_segments()):
        raise ArmBusyError(
            f"{iface} 의 명령 세그먼트를 누가 이미 쥐고 있습니다 — "
            "추론이나 녹화가 도는 중인지 보세요")
    return shm_arm.ActionWriter(iface, deadman_ms=deadman_ms)


def require_healthy_bus(iface: str) -> None:
    """버스가 나쁘면 시작을 막는다. `ArmBusyError` 로 사유를 말한다.

    ⚠ 여기서 안 막으면 조그가 열리고 슬라이더도 움직이는데 **팔만 안 움직인다.**
    SDK 가 전송 실패를 조용히 삼키기 때문이다 — 사용자는 소프트웨어를 의심하게
    된다. 시작하는 순간이 그걸 말해줄 가장 좋은 때다.
    """
    from piper_robot.can import can_unhealthy_reason

    bad = can_unhealthy_reason(iface)
    if bad:
        raise ArmBusyError(bad)


def enable_torque(iface: str) -> None:
    """조작 전에 토크를 켠다. **안 켜면 명령이 나가도 팔이 힘을 안 쓴다.**

    ⚠ 관절 명령 경로(shm → robotd)는 토크를 안 건드린다 — 추론 프록시가 자기
    연결 시점에 켜는 것을 전제로 만들어졌기 때문이다. 조그·릴레이는 그 프록시가
    없으므로 여기서 켠다. 실기에서 "명령은 가는데 안 움직인다"로 걸렸다.

    실패해도 막지 않는다. 토크가 이미 켜져 있을 수도 있고, 못 켰다면 어차피
    안 움직이는 것으로 사용자가 안다 — 시작 자체를 거절하면 이유가 더 흐려진다.
    """
    from app.services.robot_manager import robot_manager

    arm = robot_manager.arms.get(iface)
    if arm is None:
        return
    try:
        if not arm.enable_torque():
            logger.warning("%s 토크를 켜지 못했습니다 — 명령이 나가도 안 움직일 수 있습니다",
                           iface)
    except Exception as exc:
        logger.warning("%s 토크 켜기 실패: %s", iface, exc)


def close_action_writer(writer, iface: str | None) -> None:
    """닫고 **세그먼트를 지운다** — 브리지가 "소비자 종료"로 처리한다.

    남겨두면 발행자 없는 세그먼트가 되어, 게이트웨이의 장치 감시가
    "발행이 멈췄다"로 읽는다.
    """
    from piper_shm import arm as shm_arm

    if writer is not None:
        try:
            writer.close()
        except Exception as exc:
            logger.warning("명령 라이터 닫기 실패: %s", exc)
    if iface:
        shm_arm.unlink(shm_arm.segment_name(iface, shm_arm.KIND_ACTION))
