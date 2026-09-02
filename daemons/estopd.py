#!/usr/bin/env python3
"""piper-estopd — E-stop 워치독. **웹서버와 독립된 프로세스.**

CLAUDE.md / REF.md 가 *"E-stop 은 웹서버와 반드시 분리된 독립 watchdog 프로세스"* 를
설계 원칙으로 못 박았지만, 실제로는 `asyncio.create_task` 로 **게이트웨이와 같은
이벤트 루프**에서 돌고 있었다. 루프가 막히면 워치독도 같이 멈춘다 —
D405 UVC 컨트롤 질의가 커널 D-state 로 이벤트 루프 전체를 먹통으로 만든 전례가 있고,
그 순간 E-stop 은 죽어 있었다.

## 왜 PID 를 직접 죽이는가

버스로 "정지해줘"를 보내고 게이트웨이가 실행하게 하면 **아무것도 나아지지 않는다** —
게이트웨이가 응답 못 하는 상황이 바로 이 데몬이 존재하는 이유다.
그래서 게이트웨이가 Redis 에 올려둔 활동 PID 를 읽어 **직접 SIGKILL** 한다.

게이트웨이는 알림(`CH_ESTOP`)만 구독해 UI 에 표시한다. 정지는 이미 끝나 있다.

## 실행

    python daemons/estopd.py                       # 개발
    systemctl --user start piper-estopd            # 배포 (systemd 유닛)

환경변수: `PIPER_REDIS_URL`, `PIPER_ESTOP_TIMEOUT_S`, `PIPER_ESTOP_POLL_S`
"""

import logging
import os
import signal
import sys
import time

from pathlib import Path

from piper_bus import Bus
from piper_bus import contract as C
from piper_bus.client import self_report

REPO = Path(__file__).resolve().parents[1]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] estopd: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def kill_pid(pid: int) -> bool:
    """즉시 SIGKILL. graceful stop 이 아니다 — E-stop 이다."""
    try:
        os.kill(pid, signal.SIGKILL)
        logger.warning("SIGKILL → pid=%d", pid)
        return True
    except ProcessLookupError:
        return False  # 이미 죽음
    except PermissionError:
        logger.error("pid=%d 를 죽일 권한이 없습니다 — 같은 사용자로 실행해야 합니다", pid)
        return False


def trigger(bus: Bus, reason: str) -> list[str]:
    """로봇을 움직이는 활동을 전부 즉시 죽인다. 정지시킨 활동 목록을 돌려준다.

    하나가 실패해도 나머지는 계속 — 부분 성공이라도 해야 한다.
    """
    pids = bus.activity_pids()
    stopped, killed = [], []
    for activity, pid in pids.items():
        if kill_pid(pid):
            stopped.append(activity)
            killed.append(pid)
        bus.set_activity_pid(activity, None)
    bus.set_armed(False)
    bus.record_estop(reason, stopped, killed)
    if stopped:
        logger.warning("E-STOP (%s) → 정지: %s", reason, ", ".join(stopped))
    else:
        logger.warning("E-STOP (%s) → 정지할 프로세스 없음", reason)
    return stopped


def watch(bus: Bus, timeout_s: float, poll_s: float) -> None:
    logger.info(
        "watchdog 시작 (timeout=%.1fs poll=%.2fs redis=%s)",
        timeout_s, poll_s, os.environ.get("PIPER_REDIS_URL", C.DEFAULT_REDIS_URL),
    )
    warned_disconnect = False
    while True:
        time.sleep(poll_s)
        try:
            # 게이트웨이가 컨테이너 안이면 systemd 를 못 보므로, 생존은 이 키가 전부다
            bus.mark_alive(C.ESTOPD, info=self_report(REPO, C.DAEMON_SOURCES[C.ESTOPD]))
            if not bus.is_armed():
                warned_disconnect = False
                continue
            last = bus.last_beat()
            if last is None:
                continue
            elapsed = time.time() - last
            if elapsed > timeout_s:
                logger.warning("heartbeat 끊김 (%.1fs > %.1fs)", elapsed, timeout_s)
                trigger(bus, C.ESTOP_REASON_TIMEOUT)
            warned_disconnect = False
        except Exception as e:
            # Redis 가 죽어도 데몬은 살아 있어야 한다 — 복구되면 감시를 재개한다.
            if not warned_disconnect:
                logger.error("버스 오류, 재시도: %s", e)
                warned_disconnect = True


def main() -> int:
    bus = Bus()
    if not bus.ping():
        logger.error("Redis 에 연결할 수 없습니다: %s", os.environ.get("PIPER_REDIS_URL", C.DEFAULT_REDIS_URL))
        return 1

    def _bye(signum, _frame):
        logger.info("종료 (signal %s)", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    try:
        watch(
            bus,
            _env_float("PIPER_ESTOP_TIMEOUT_S", C.DEFAULT_ESTOP_TIMEOUT_S),
            _env_float("PIPER_ESTOP_POLL_S", C.DEFAULT_ESTOP_POLL_S),
        )
    except SystemExit:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
