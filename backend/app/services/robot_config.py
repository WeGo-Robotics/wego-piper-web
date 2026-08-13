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
        # ⚠ 발행을 여기서 켜지 않는다. robotd 가 **연결과 동시에** 시작한다 —
        # 게이트웨이가 또 켜면 같은 세그먼트에 발행자가 둘이 되고, 그건
        # seqlock 이 막지 못하는 종류의 손상이다.
        logger.info("팔 shm 발행 중(%s): %s", purpose, iface)


def release_arms(ifaces: list[str] | None = None) -> None:
    """추론·녹화가 끝나도 **발행은 내리지 않는다.**

    robotd 는 팔에 연결돼 있는 한 계속 발행한다 — 카메라와 같다. 소비자가 떠나면
    명령 세그먼트가 사라지고 데몬이 그걸 알아채는 것으로 충분하다.
    끊어야 할 이유가 생기면 `robot_manager.disconnect_arm()` 이 그 일을 한다.

    함수를 남겨두는 이유는 호출부(`direct` 경로 정리)가 대칭을 기대하기 때문이다.
    """
    return
