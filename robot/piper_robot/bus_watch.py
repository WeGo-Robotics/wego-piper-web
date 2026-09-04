"""버스 표본 수집기 — robotd 안에서 계속 돈다.

⚠ **이력을 브라우저가 모으면 탭을 열 때마다 빈 그래프로 시작한다.** 표본이
쌓이길 기다려야 하고, 탭을 닫으면 그동안의 기록이 사라진다. 정작 보고 싶은
것은 "**내가 안 보고 있던 동안** 무슨 일이 있었나" 인데 그때가 비어 있는 셈이다.
그래서 데몬이 잰다 — CAN 을 쥔 쪽이고, 계속 살아 있는 쪽이다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from piper_robot.can import bus_stats, scan_can_interfaces

logger = logging.getLogger(__name__)

#: 표본 간격 (초).
INTERVAL_S = 2.0
#: 버스별 보관 표본 수. 2초 × 150 = 5분.
HISTORY = 150


class BusWatch:
    """버스별 초당 증가량을 계속 기록한다."""

    def __init__(self) -> None:
        self._hist: dict[str, deque] = {}
        self._prev: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="buswatch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def clear(self, iface: str) -> None:
        """이 버스의 기록을 버린다 — **초기화가 기준선을 새로 잡을 때** 부른다.

        ⚠ 초기화 전 표본은 다른 기준의 값이다. 남겨 두면 그래프가 두 기준을 한
        선에 섞어 그리고, 화면의 다른 숫자는 전부 "초기화 이후" 인데 그래프만
        옛 구간을 보여준다 — 섞인 기준은 틀린 값보다 나쁘다.
        """
        with self._lock:
            self._hist.pop(iface, None)
            self._prev.pop(iface, None)

    def history(self, iface: str) -> list[dict]:
        with self._lock:
            return list(self._hist.get(iface, ()))

    # ── 내부 ──

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._sample()
            except Exception as exc:                      # noqa: BLE001
                logger.debug("버스 표본 실패: %s", exc)
            # 재는 데 걸린 만큼 빼고 잔다 — 안 그러면 간격이 조금씩 밀린다
            self._stop.wait(max(0.1, INTERVAL_S - (time.monotonic() - t0)))

    def _sample(self) -> None:
        now = time.time()
        for c in scan_can_interfaces():
            iface = c["iface"]
            cur = bus_stats(iface)
            prev = self._prev.get(iface)
            self._prev[iface] = (now, cur)
            if prev is None:
                continue
            dt = now - prev[0]
            # ⚠ 간격이 크게 벌어졌으면(데몬이 멈췄다 깼다) 그 구간은 버린다 —
            #   평균이 뭉개져 "그동안 조용했다" 로 보인다.
            if not (INTERVAL_S * 0.5 <= dt <= INTERVAL_S * 5):
                continue
            self._push(iface, {
                "t": round(now, 2),
                "rx": _rate(cur.get("rx_packets"), prev[1].get("rx_packets"), dt),
                "tx": _rate(cur.get("tx_packets"), prev[1].get("tx_packets"), dt),
                "err": _rate(cur.get("errors_total"), prev[1].get("errors_total"), dt),
            })

    def _push(self, iface: str, row: dict) -> None:
        with self._lock:
            self._hist.setdefault(iface, deque(maxlen=HISTORY)).append(row)


def _rate(now, before, dt: float) -> float:
    """초당 증가량. 못 읽었거나 카운터가 되감겼으면 0 — 음수 속도는 없다."""
    if now is None or before is None or dt <= 0:
        return 0.0
    return round(max(0.0, (now - before) / dt), 2)


bus_watch = BusWatch()
