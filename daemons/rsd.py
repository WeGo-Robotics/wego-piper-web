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
import threading
import time

from pathlib import Path

from piper_bus import contract as C
from piper_bus.client import Bus, self_report
from piper_rs import RealSenseHub, rs_available
from piper_rs import publish

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] rsd: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("rsd")

REPO = Path(__file__).resolve().parents[1]

_running = True

# 게이트웨이가 부르는 것만 노출한다. 여기 없는 메서드는 거부된다 —
# 데몬을 임의 호출 창구로 만들지 않는다.
_METHODS = {
    "scan", "connect", "disconnect", "release_all", "is_d405",
    "probe", "hardware_reset", "list_controls", "set_control",
    "info", "set_depth_encoding", "set_background_mask", "calibrate_gray_card", "measure_gray_card",
    "apply_controls", "last_apply_report", "lost",
}


def heartbeat(bus: Bus, name: str, stop: threading.Event) -> None:
    """생존 표시를 **RPC 루프와 따로** 낸다.

    ⚠ **긴 RPC 하나가 데몬을 죽은 것으로 만든다.** 루프 안에서 표시하면 그
      표시가 처리 시간만큼 늦고, 게이트웨이의 판정 기준(`DAEMON_ALIVE_TTL_MS`)은
      3초다. 실측으로 넘긴 것:

          회색 카드 보정 = 안정화 2.0초 + 자동끄기 0.4초 + 3라운드 × 0.4초
                        = **최소 3.6초** > 3.0초

      그래서 보정할 때마다 "rsd 응답 없음"이 떴다 — 데몬은 멀쩡히 일하는 중인데.
      로그에 그 상관이 그대로 남아 있다(11:04:05 응답없음 → 11:04:06 보정 완료).

      느린 것과 죽은 것은 다르고, 그 둘을 섞으면 진짜 죽었을 때를 못 알아본다.
    """
    period = C.DAEMON_ALIVE_TTL_MS / 3000
    while not stop.wait(period):
        try:
            bus.mark_alive(name, info=self_report(REPO, C.DAEMON_SOURCES[name]))
        except Exception as exc:
            logger.warning("생존 표시 실패: %s", exc)


def serve(bus: Bus, hub: RealSenseHub) -> None:
    global _running
    logger.info("RealSense 데몬 시작 (pyrealsense2=%s)", rs_available())
    stop = threading.Event()
    threading.Thread(target=heartbeat, args=(bus, C.RSD, stop),
                     daemon=True, name="rsd-beat").start()
    while _running:
        try:
            req = bus.rpc_next_request(C.RSD, timeout=1)
        except Exception as exc:
            logger.warning("요청 수신 오류: %s", exc)
            time.sleep(0.5)
            continue
        if req is None:
            continue

        rid, method, args = req.get("id", ""), req.get("method", ""), req.get("args", [])
        if method == "restart":
            # 컨테이너 게이트웨이는 systemctl 이 없다 — 스스로 죽으면
            # 유닛의 Restart=always 가 새 코드로 되살린다. 응답이 먼저다.
            bus.rpc_reply(rid, True, result="restarting")
            logger.warning("재시작 요청 — 정리 후 종료")
            _running = False
            continue
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
    stop.set()


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
