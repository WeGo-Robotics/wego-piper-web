#!/usr/bin/env python3
"""piper-simd — MuJoCo 세계를 드는 데몬 (feature/sim-env.md).

시뮬레이터는 "또 하나의 로봇 데몬"이다: robotd 와 같은 팔 shm 계약을 발행한다.
실장치 데몬에 sim 모드를 넣지 않는다 — 실기에서 시뮬 코드가 돌 자리를 안 만든다.

환경변수: `PIPER_REDIS_URL`, `MUJOCO_GL`(렌더는 2단계)
"""

import logging
import os
import signal
import sys
import time
from pathlib import Path

from piper_bus import contract as C
from piper_bus.client import Bus, self_report
from piper_sim.hub import SimHub

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] simd: %(message)s", stream=sys.stdout)
logger = logging.getLogger("simd")
REPO = Path(__file__).resolve().parents[1]
_running = True

_METHODS = {"scan", "attach", "release", "release_all", "estop", "info", "lost",
            "cube_pos", "reset_cube", "set_light", "go_to",
            # 카메라 — camerad 어휘에 cam_ 접두사
            "cam_scan", "cam_connect", "cam_disconnect", "cam_release_all", "cam_probe",
            "cam_list_controls", "cam_set_control", "cam_apply_controls",
            "cam_last_apply_report", "cam_info", "cam_lost"}


def serve(bus: Bus, hub: SimHub) -> None:
    global _running
    logger.info("시뮬 데몬 시작")
    last_beat = 0.0
    while _running:
        now = time.monotonic()
        if now - last_beat > C.DAEMON_ALIVE_TTL_MS / 3000:
            try:
                bus.mark_alive(C.SIMD, info=self_report(REPO, C.DAEMON_SOURCES[C.SIMD]))
            except Exception as exc:
                logger.warning("생존 표시 실패: %s", exc)
            last_beat = now
        try:
            req = bus.rpc_next_request(C.SIMD, timeout=1)
        except Exception as exc:
            logger.warning("요청 수신 오류: %s", exc)
            time.sleep(0.5)
            continue
        if req is None:
            continue
        rid, method, args = req.get("id", ""), req.get("method", ""), req.get("args", [])
        if method == "restart":
            bus.rpc_reply(rid, True, result="restarting")
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
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    bus = Bus()
    if not bus.ping():
        logger.error("Redis 에 연결할 수 없습니다 (%s)", os.environ.get("PIPER_REDIS_URL", "기본값"))
        return 1
    hub = SimHub()
    try:
        from piper_shm import arm as A
        from piper_shm import list_segments as cam_segments, unlink as cam_unlink
        stale = [n for n in A.list_segments() if ".sim_" in n]
        for n in stale:
            A.unlink(n)
        stale_cam = [n for n in cam_segments() if n.startswith("sim_")]
        for n in stale_cam:
            cam_unlink(n)
        stale += stale_cam
        if stale:
            logger.info("남은 sim 세그먼트 %d개 정리: %s", len(stale), stale)
    except Exception as exc:
        logger.warning("세그먼트 정리 실패: %s", exc)
    try:
        serve(bus, hub)
    finally:
        hub.cameras.stop()          # 세그먼트를 남기면 소비자가 멈춘 화면을 본다
        hub.release_all()
        if hub.world:
            hub.world.stop()
        logger.info("종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
