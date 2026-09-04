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
#: 버스별 보관 표본 수. 2초 × 900 = **30분** — 화면이 고를 수 있는 가장 긴 창.
#:
#: ⚠ **항상 최대치를 모은다.** 화면이 고른 만큼만 모으면, 5분으로 보다가 30분으로
#:   바꾸는 순간 그 25분이 비어 있다 — 정작 "아까 뭐였지" 를 보려고 바꾸는 것인데.
#:   보관은 싸고(한 표본 4개 숫자), 뒤늦게 되돌릴 수 없는 쪽은 안 모은 시간이다.
HISTORY = 900


class BusWatch:
    """버스별 초당 증가량을 계속 기록한다."""

    def __init__(self) -> None:
        self._hist: dict[str, deque] = {}
        self._prev: dict[str, tuple[float, dict]] = {}
        # ⚠ **기준선을 수집기가 들고 있다.** 이력과 같은 자리에 둬야 둘이 함께
        #   새로 잡힌다 — 숫자는 초기화됐는데 그래프만 옛 구간을 보여주는 일이
        #   생기지 않는다.
        self._base: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # ⚠ **기동하면 기준선을 잡는다.** CAN 오류 카운터는 커널 쪽이라 데몬을
        #   다시 띄워도 그대로다 — 안 잡으면 새로 뜬 데몬이 어제의 1억을 물려받아
        #   보여준다. 데몬이 새로 떴다는 것은 "여기서부터 본다" 는 뜻이다.
        self.rebase()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="buswatch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def rebase(self, iface: str | None = None) -> None:
        """지금을 기준으로 삼는다 — 이력도 함께 버린다.

        `iface` 가 없으면 전부. 데몬 기동과 버스 초기화가 이걸 부른다.
        """
        for c in scan_can_interfaces():
            name = c["iface"]
            if iface and name != iface:
                continue
            try:
                cur = bus_stats(name)
            except Exception:
                continue
            with self._lock:
                self._base[name] = {
                    "counters": dict(cur.get("counters") or {}),
                    "rx_packets": cur.get("rx_packets") or 0,
                    "tx_packets": cur.get("tx_packets") or 0,
                }
                self._hist.pop(name, None)
            self._prev.pop(name, None)

    def baseline(self, iface: str) -> dict | None:
        with self._lock:
            return self._base.get(iface)

    def clear(self, iface: str) -> None:
        """이 버스의 기록을 버린다 — **초기화가 기준선을 새로 잡을 때** 부른다.

        ⚠ 초기화 전 표본은 다른 기준의 값이다. 남겨 두면 그래프가 두 기준을 한
        선에 섞어 그리고, 화면의 다른 숫자는 전부 "초기화 이후" 인데 그래프만
        옛 구간을 보여준다 — 섞인 기준은 틀린 값보다 나쁘다.
        """
        with self._lock:
            self._hist.pop(iface, None)
            self._prev.pop(iface, None)

    def history(self, iface: str, limit: int | None = None) -> list[dict]:
        """최근 표본. `limit` 을 주면 그만큼만 — 화면이 고른 창이다.

        ⚠ 잘라 보내는 이유는 payload 다. 30분치(900개) × 네 버스를 2초마다
        보내면 대부분 안 그리는 점이다.
        """
        with self._lock:
            rows = list(self._hist.get(iface, ()))
        return rows[-limit:] if limit else rows

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
