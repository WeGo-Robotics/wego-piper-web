#!/usr/bin/env python3
"""piper-robotd — CAN 을 독점하는 데몬 (daemon-inventory.md #2).

## 팔에 명령하는 넷이 전부 여기를 지난다

정책 추론 · 텔레오퍼레이션 녹화 · 웹 수동 제어 · 파킹. 안전 필터를 프록시
드라이버에 두면 LeRobot 경로만 보호되고 나머지 셋은 무방비다. CAN 을 쥔 쪽에
두면 나가는 명령이 전부 한 곳을 통과한다 (refactor/robotd-safety.md).

## 역할

- **상태**: 연결된 팔의 관절을 `/dev/shm` 에 **항상** 발행한다
- **명령**: 소비자가 shm 에 쓴 목표를 안전층에 통과시켜 CAN 으로 보낸다
- **데드맨**: 소비자가 죽거나 멈추면 **현재 자세를 명령해서** 팔을 세운다.
  E-stop watchdog(estopd)과 별개로, CAN 을 쥔 프로세스 안의 최종 방어선이다
- **제어**: 버스 RPC (스캔·연결·토크·파킹·에러·모션감지·USB 복구)

## 왜 호스트인가

`recover_usb_controllers` 가 `/sys/bus/pci/drivers/xhci_hcd/{unbind,bind}` 에 쓴다.
컨테이너에서 하려면 호스트 권한을 다 줘야 하므로 systemd 유닛으로 간다.

환경변수: `PIPER_REDIS_URL`, `PIPER_CONFIG_DIR`
"""

import logging
import os
import signal
import sys
import time

from piper_bus import contract as C
from piper_bus.client import Bus
from piper_robot import publish
from piper_robot.hub import RobotHub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] robotd: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("robotd")

_running = True

# 게이트웨이가 부르는 것만 노출한다 — 데몬을 임의 호출 창구로 만들지 않는다.
_METHODS = {
    "scan", "connect", "disconnect", "release_all", "info",
    "refresh_mode", "set_master_slave", "read_joints", "read_joints_raw",
    "clear_errors", "read_error", "enable_torque", "disable_torque", "go_parking",
    "start_motion_detect", "motion_status", "start_identify",
    "init_interface", "check_active", "sniff_ids", "rename_interface", "recover_usb", "usb_info",
    "lost",
}


class _Serving(RobotHub):
    """허브에 **발행 수명**을 붙인다.

    ⚠ 연결하면 곧바로 발행을 시작한다 — 소비자가 붙기를 기다리지 않는다.
    상태 세그먼트가 있어야 소비자가 연결할 수 있으므로 순서가 반대면 아무도 못 붙는다.
    """

    def connect(self, iface: str) -> tuple[bool, str]:
        ok, msg = super().connect(iface)
        if ok:
            arm = self.arms.get(iface)
            if arm is not None:
                publish.arm_bridge_manager.start(arm)
        return ok, msg

    def disconnect(self, iface: str) -> bool:
        # ⚠ **발행을 먼저 멈춘다.** CAN 을 먼저 닫으면 그 사이 발행 루프가
        # 죽은 핸들을 읽어 예외를 뱉는다.
        publish.arm_bridge_manager.stop(iface)
        return super().disconnect(iface)

    def release_all(self) -> bool:
        publish.arm_bridge_manager.stop_all()
        return super().release_all()

    def lost(self) -> list[dict]:
        """**데몬이 판정한** 사라진 팔들.

        게이트웨이가 세그먼트 나이로 추론하면 늦고, 애초에 컨테이너 안에서는
        `/sys/class/net` 이 안 보여 인터페이스 소멸을 알 수도 없다
        (브리지 네트워크). 판정이 여기 있어야 하는 이유다.
        """
        return publish.arm_bridge_manager.lost()


def serve(bus: Bus, hub: _Serving) -> None:
    logger.info("로봇 데몬 시작")
    last_beat = 0.0
    while _running:
        now = time.monotonic()
        if now - last_beat > C.DAEMON_ALIVE_TTL_MS / 3000:
            try:
                bus.mark_alive(C.ROBOTD)
            except Exception as exc:
                logger.warning("생존 표시 실패: %s", exc)
            last_beat = now

        try:
            req = bus.rpc_next_request(C.ROBOTD, timeout=1)
        except Exception as exc:
            logger.warning("요청 수신 오류: %s", exc)
            time.sleep(0.5)
            continue
        if req is None:
            continue

        rid, method, args = req.get("id", ""), req.get("method", ""), req.get("args", [])
        if method not in _METHODS:
            bus.rpc_reply(rid, False, error=f"알 수 없는 메서드: {method}")
            continue
        try:
            bus.rpc_reply(rid, True, result=getattr(hub, method)(*args))
        except Exception as exc:
            logger.warning("%s 실패: %s", method, exc)
            bus.rpc_reply(rid, False, error=str(exc))


def main() -> int:
    def _bye(signum, _frame):
        global _running
        _running = False
        logger.info("신호 %s — 정리 중", signum)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    bus = Bus()
    if not bus.ping():
        logger.error("Redis 에 연결할 수 없습니다 (%s)",
                     os.environ.get("PIPER_REDIS_URL", "기본값"))
        return 1

    hub = _Serving()
    # ⚠ 지난 프로세스가 죽으면 `/dev/shm` 세그먼트가 남는다. 치우지 않으면 소비자가
    # 발행자 없는 세그먼트를 열고 **멈춘 자세**를 관측으로 받는다 — 그건 그럴듯해 보인다.
    try:
        from piper_shm import arm as A

        stale = A.list_segments()
        for name in stale:
            A.unlink(name)
        if stale:
            logger.info("남은 팔 세그먼트 %d개 정리: %s", len(stale), stale)
    except Exception as exc:
        logger.warning("세그먼트 정리 실패: %s", exc)

    # ⚠ 기동 시 한 번 스캔한다 — 안 하면 `connect` 가 장치를 못 찾는다 (rsd 에서 겪었다)
    try:
        found = hub.scan()
        logger.info("CAN %d개 발견: %s", len(found), [d["iface"] for d in found])
    except Exception as exc:
        logger.warning("초기 스캔 실패: %s", exc)

    try:
        serve(bus, hub)
    finally:
        # ⚠ 세그먼트를 남기면 소비자가 그걸 열고 **멈춘 자세**를 본다
        publish.arm_bridge_manager.stop_all()
        try:
            hub.release_all()
        except Exception:
            pass
        logger.info("종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
