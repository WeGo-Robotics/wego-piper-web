"""전송 방식에 맞춰 로봇을 준비하고 `robot.type` 을 정한다.

`camera_config.py` 와 **같은 자리, 같은 모양**이다 — 카메라와 로봇이 같은 전환을
겪고 있고, 둘 다 "게이트웨이가 장치를 쥔 채로 wrapper 에게 넘긴다"로 바뀌는 중이다.
한쪽만 다른 모양이면 다음 사람이 두 규칙을 외워야 한다.

| | `direct` | `shm` |
|---|---|---|
| `robot.type` | `piper_follower` | `piper_follower_shm` |
| CAN 을 여는 쪽 | wrapper(subprocess) | 게이트웨이(나중엔 robotd) |
| 시작 전 할 일 | 에러 클리어 타이밍 맞추기 | 브리지 켜기 |

`shm` 에서 CAN 경합이 사라지므로 `_clear_arm_errors` 의 "기동 전 / 정지 후"
순서 프로토콜도 의미가 없어진다 (robot-transport.md "얻는 것" #1).
"""

import logging

from app.core.cli_mapping import resolve_robot_type
from app.core.config import settings
from app.services.robot_manager import robot_manager

logger = logging.getLogger(__name__)

__all__ = ["ArmPrepareError", "prepare_arms", "release_arms", "resolve_robot_type"]


class ArmPrepareError(RuntimeError):
    """팔을 준비할 수 없다 — 사람이 읽을 사유를 담는다."""


def prepare_arms(ifaces: list[str] | None, *, purpose: str) -> None:
    """전송 방식에 맞게 팔을 준비한다.

    - `direct`: 아무것도 안 한다. wrapper 가 CAN 을 직접 연다
    - `shm`: 게이트웨이가 CAN 을 **쥔 채로** 상태를 발행하고 명령을 받는다.
      카메라의 `prepare_cameras` 와 같은 뒤바뀜이다 — 놓는 게 아니라 잡는다
    """
    if settings.robot_transport != "shm" or not ifaces:
        return

    from app.services.arm_bridge import arm_bridge_manager

    for iface in ifaces:
        if not iface:
            continue
        arm = robot_manager.arms.get(iface)
        if arm is None:
            raise ArmPrepareError(f"팔을 찾을 수 없습니다: {iface}")
        if not arm.connected:
            ok, msg = arm.connect()
            if not ok:
                raise ArmPrepareError(f"팔 연결 실패 ({iface}): {msg}")
        bridge = arm_bridge_manager.start(arm)
        logger.info("팔 shm 발행 중(%s): %s (발행 %d)", purpose, iface, bridge.published)


def release_arms(ifaces: list[str] | None = None) -> None:
    """추론·녹화가 끝난 뒤 브리지를 내린다.

    브리지를 켜 둔 채로 두면 게이트웨이가 계속 CAN 으로 명령을 보낼 수 있는 상태가
    남는다 — 소비자가 떠났으면 그 통로도 닫는 게 맞다.
    """
    if settings.robot_transport != "shm":
        return

    from app.services.arm_bridge import arm_bridge_manager

    if ifaces is None:
        arm_bridge_manager.stop_all()
        return
    for iface in ifaces:
        if iface:
            arm_bridge_manager.stop(iface)
