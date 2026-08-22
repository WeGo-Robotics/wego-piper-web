#!/usr/bin/env python3
"""piper-rsd — RealSense 파이프라인을 독점하는 데몬 (daemon-inventory.md #4).

## 왜 따로 떼나

D405 의 UVC 컨트롤 질의가 커널 D-state 로 **이벤트 루프 전체를 먹통**으로 만든
전례가 있다. 게이트웨이 안에 있으면 그 순간 웹도, 프리뷰도, 다른 카메라도 같이 멈춘다.
프로세스를 나누면 RealSense 가 죽어도 웹캠과 웹 UI 는 산다 —
그래서 camerad(v4l2)와도 **합치지 않고 따로** 둔다.

## 역할 분담

- **프레임**: `/dev/shm` 세그먼트에 **항상** 발행한다. 누가 어떤 키로 쓸지는 모른다
- **제어**: 버스 RPC (스캔·연결·컨트롤). 요청/응답이 필요한 것만
- 프리뷰는 RPC 가 아니다 — 게이트웨이가 세그먼트에서 직접 읽는다
  (그래서 `preview_bridge` 가 필요 없어진다)

## 소유권 중재가 없다

camerad/rsd 가 장치를 계속 쥐고 소비자는 shm 만 읽으므로 **소유권이 이전되지 않는다.**
lease 협상도, `_release_all_cameras()` 같은 해제 춤도 필요 없다
(refactor/daemon-inventory.md "장치 소유권 중재 — 해소됨").

환경변수: `PIPER_REDIS_URL`
"""

import logging
import os
import signal
import sys
import time

from piper_bus import contract as C
from piper_bus.client import Bus
from piper_rs import RealSenseHub, rs_available
from piper_rs import publish

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] rsd: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("rsd")

_running = True

# 게이트웨이가 부르는 것만 노출한다. 여기 없는 메서드는 거부된다 —
# 데몬을 임의 호출 창구로 만들지 않는다.
_METHODS = {
    "scan", "connect", "disconnect", "release_all", "is_d405",
    "probe", "hardware_reset", "list_controls", "set_control",
    "info", "set_depth_encoding", "set_background_mask",
    "apply_controls", "last_apply_report", "lost",
}


def serve(bus: Bus, hub: RealSenseHub) -> None:
    logger.info("RealSense 데몬 시작 (pyrealsense2=%s)", rs_available())
    last_beat = 0.0
    while _running:
        # 생존 표시 — 게이트웨이가 "데몬 없음"을 즉시 알아야 한다
        now = time.monotonic()
        if now - last_beat > C.DAEMON_ALIVE_TTL_MS / 3000:
            try:
                bus.mark_alive(C.RSD)
            except Exception as exc:
                logger.warning("생존 표시 실패: %s", exc)
            last_beat = now

        try:
            req = bus.rpc_next_request(C.RSD, timeout=1)
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
            result = getattr(hub, method)(*args)
            # tuple 은 JSON 에서 list 가 된다 — 호출부가 인덱싱하므로 그대로 둔다
            bus.rpc_reply(rid, True, result=result)
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
        logger.error("Redis 에 연결할 수 없습니다 (%s)", os.environ.get("PIPER_REDIS_URL", "기본값"))
        return 1

    hub = RealSenseHub()
    # ⚠ **기동 시 한 번 스캔한다.** 안 하면 장치 목록이 비어 있어 `connect` 가
    # "not found (rescan)" 로 실패한다. 게이트웨이 시절에는 카메라 페이지가
    # 스캔을 먼저 불러서 가려져 있던 순서 의존이다.
    # ⚠ 지난 프로세스가 죽으면 `/dev/shm` 세그먼트가 남는다. 치우지 않으면 소비자가
    # 발행자 없는 세그먼트를 열어 **멈춘 화면**을 본다.
    # **자기 것만** 치운다 — 다른 데몬이 발행 중인 것을 지우면 그쪽이 깨진다.
    try:
        from piper_shm import list_segments, unlink

        stale = [n for n in list_segments() if n.startswith("rs_")]
        for name in stale:
            unlink(name)
        if stale:
            logger.info("남은 세그먼트 %d개 정리: %s", len(stale), stale)
    except Exception as exc:
        logger.warning("세그먼트 정리 실패: %s", exc)

    try:
        found = hub.scan()
        logger.info("장치 %d개 발견: %s", len(found), [d["id"] for d in found])
    except Exception as exc:
        logger.warning("초기 스캔 실패: %s", exc)

    try:
        serve(bus, hub)
    finally:
        # ⚠ 세그먼트를 남기면 소비자가 그걸 열고 **멈춘 화면**을 본다.
        # 파일이 있으니 연결은 성공하는데 발행자가 없어 프레임이 안 온다.
        publish.stop_all()
        try:
            hub.release_all()
        except Exception:
            pass
        logger.info("종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
