#!/usr/bin/env python3
"""piper-camerad — `/dev/video*` (v4l2) 를 독점하는 데몬 (daemon-inventory.md #3).

## rsd 와 합치지 않는다

D405 의 UVC 컨트롤 질의가 커널 D-state 로 프로세스를 통째로 먹통으로 만든 전례가 있다.
합쳐두면 RealSense 가 죽을 때 웹캠까지 같이 죽는다 — 나눠서 얻는 것이 그 격리다.
**소유가 겹치지 않는 것도 중요하다**: camerad 는 RealSense 노드를 무조건 건너뛰고
(`piper_cam.v4l2._scan_one`), rsd 는 v4l2 를 안 본다.

## 역할

- **프레임**: 연결된 카메라를 `/dev/shm` 세그먼트에 **항상** 발행한다
- **제어**: 버스 RPC (스캔·연결·컨트롤). 프레임은 RPC 로 다니지 않는다

환경변수: `PIPER_REDIS_URL`
"""

import logging
import os
import signal
import sys
import time

from piper_bus import contract as C
from piper_bus.client import Bus
from piper_cam import V4l2Hub
from piper_cam import publish

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] camerad: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("camerad")

_running = True

# 게이트웨이가 부르는 것만 노출한다 — 데몬을 임의 호출 창구로 만들지 않는다.
_METHODS = {
    "scan", "connect", "disconnect", "release_all",
    "probe", "list_controls", "set_control", "info",
    "apply_controls", "last_apply_report",
}


def serve(bus: Bus, hub: V4l2Hub) -> None:
    logger.info("v4l2 데몬 시작")
    last_beat = 0.0
    while _running:
        now = time.monotonic()
        if now - last_beat > C.DAEMON_ALIVE_TTL_MS / 3000:
            try:
                bus.mark_alive(C.CAMERAD)
            except Exception as exc:
                logger.warning("생존 표시 실패: %s", exc)
            last_beat = now

        try:
            req = bus.rpc_next_request(C.CAMERAD, timeout=1)
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

    hub = V4l2Hub()
    # ⚠ 기동 시 한 번 스캔한다 — 안 하면 `connect` 가 장치를 못 찾는다 (rsd 에서 겪었다)
    # ⚠ 지난 프로세스가 죽으면 `/dev/shm` 세그먼트가 남는다. 치우지 않으면 소비자가
    # 발행자 없는 세그먼트를 열어 **멈춘 화면**을 본다.
    # **자기 것만** 치운다 — 다른 데몬이 발행 중인 것을 지우면 그쪽이 깨진다.
    try:
        from piper_shm import list_segments, unlink

        stale = [n for n in list_segments() if n.startswith("dev_")]
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
        # ⚠ 세그먼트를 남기면 소비자가 그걸 열고 **멈춘 화면**을 본다
        publish.stop_all()
        try:
            hub.release_all()
        except Exception:
            pass
        logger.info("종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
