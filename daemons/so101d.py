#!/usr/bin/env python3
"""piper-so101d — SO-101 (Feetech 시리얼 버스) 팔을 독점하는 데몬 (feature/so101d.md).

## robotd 와 합치지 않는다

Feetech 시리얼이 걸리면 죽는 것이 **이 유닛 하나**여야 한다 — CAN 제어(robotd)는
살아야 한다. rsd/camerad 를 나눈 것과 같은 격리다.

## 역할

- **상태**: 연결된 팔의 관절을 `/dev/shm` 팔 세그먼트(표준 7f 계약)로 항상 발행
- **명령**: action 세그먼트 소비 (deadman·스텝 클램프) — 리더 용법에서는 조용하다
- **제어**: 버스 RPC (데몬 계약 동사 — scan/attach/release/estop/info/lost)

환경변수: `PIPER_REDIS_URL`, `PIPER_SO101_CALIB_DIR`(선택)
"""

import logging
import os
import signal
import sys
import time
from pathlib import Path

from piper_bus import contract as C
from piper_bus.client import Bus, self_report
from piper_so101.hub import So101Hub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] so101d: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("so101d")

REPO = Path(__file__).resolve().parents[1]

_running = True

# 게이트웨이가 부르는 것만 노출한다 — 데몬을 임의 호출 창구로 만들지 않는다.
_METHODS = {
    "scan", "attach", "release", "release_all", "estop", "info", "lost",
    "calib_begin", "calib_status", "calib_save", "calib_cancel", "set_side",
}


def serve(bus: Bus, hub: So101Hub) -> None:
    global _running
    logger.info("SO-101 데몬 시작")
    last_beat = 0.0
    while _running:
        now = time.monotonic()
        if now - last_beat > C.DAEMON_ALIVE_TTL_MS / 3000:
            try:
                bus.mark_alive(C.SO101D,
                               info=self_report(REPO, C.DAEMON_SOURCES[C.SO101D]))
            except Exception as exc:
                logger.warning("생존 표시 실패: %s", exc)
            last_beat = now

        try:
            req = bus.rpc_next_request(C.SO101D, timeout=1)
        except Exception as exc:
            logger.warning("요청 수신 오류: %s", exc)
            time.sleep(0.5)
            continue
        if req is None:
            continue

        rid, method, args = req.get("id", ""), req.get("method", ""), req.get("args", [])
        if method == "restart":
            bus.rpc_reply(rid, True, result="restarting")
            logger.warning("재시작 요청 — 정리 후 종료")
            _running = False
            continue
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

    hub = So101Hub()
    # 지난 프로세스가 남긴 **so101 세그먼트만** 정리 — robotd 것을 지우면 그쪽이 깨진다
    try:
        hub.sweep_stale()
    except Exception as exc:
        logger.warning("세그먼트 정리 실패: %s", exc)
    try:
        found = hub.scan()
        logger.info("어댑터 %d개 발견: %s", len(found), [d["id"] for d in found])
    except Exception as exc:
        logger.warning("초기 스캔 실패: %s", exc)

    try:
        serve(bus, hub)
    finally:
        # 세그먼트를 남기면 소비자가 멈춘 자세를 관측으로 받는다
        hub.release_all()
        logger.info("종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
