"""robotd 가 저널을 채우지 않는다 — 웹 [로그] 가 느렸던 진짜 원인 (2026-09-10 실측).

6시간에 robotd 13.5만 줄: `sudo ethtool -i` 를 2초마다 인터페이스마다 불러 pam 이
sudo 한 번에 3줄을 남겼고, can2 가 ERROR-WARNING 이라 오류 경고가 10초마다 찍혔다.
"""

import logging
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_the_can_scan_asks_ethtool_once_per_interface_and_never_with_sudo(monkeypatch):
    from piper_robot import can as C
    calls: list[list[str]] = []

    def run(cmd, sudo=False):
        calls.append(list(cmd))
        assert not sudo or cmd[0] != "ethtool", "ethtool -i 는 sudo 가 필요 없다 — pam 이 저널을 채운다"
        if cmd[:2] == ["ip", "-br"]:
            return 0, "can0  UP  <NOARP>\ncan1  UP  <NOARP>\n", ""
        if cmd[0] == "ethtool":
            return 0, f"driver: gs_usb\nbus-info: usb-0000:00:14.0-{cmd[2][-1]}\n", ""
        return 1, "", ""
    monkeypatch.setattr(C, "_run_cmd", run)
    monkeypatch.setattr(C, "_read_can_rx", lambda iface: 0)
    C._bus_info_cache.clear()
    first = C.scan_can_interfaces()
    assert [r["bus_info"] for r in first] == ["usb-0000:00:14.0-0", "usb-0000:00:14.0-1"]
    for _ in range(30):                       # bus_watch 가 2초마다 부르는 흉내
        C.scan_can_interfaces()
    assert sum(1 for c in calls if c[0] == "ethtool") == 2, "스캔마다 ethtool 을 다시 부른다"
    # 인터페이스가 빠지면 캐시도 버린다 — 다시 꽂히면 다른 포트일 수 있다
    monkeypatch.setattr(C, "_run_cmd", lambda cmd, sudo=False: (0, "can0  UP  <NOARP>\n", "") if cmd[:2] == ["ip", "-br"] else (0, "bus-info: x\n", ""))
    C.scan_can_interfaces()
    assert "can1" not in C._bus_info_cache and "can0" in C._bus_info_cache


def test_a_bus_that_keeps_erroring_is_reported_once_then_summarised(monkeypatch, caplog):
    """can2 ERROR-WARNING 으로 카운터가 계속 오르면: 처음 한 번, 5분마다 누적 요약, 멈추면 한 번."""
    from piper_robot import publish as P
    b = P.ArmBridge.__new__(P.ArmBridge)
    b.iface = "can2"; b._err_counters = None; b._err_since = 0.0; b._err_last_log = 0.0; b._err_accum = {}
    counter = {"bus_error": 0}
    monkeypatch.setattr("piper_robot.can.error_counters", lambda iface: dict(counter))
    clock = {"t": 1000.0}
    import time as _time
    monkeypatch.setattr(_time, "monotonic", lambda: clock["t"])
    caplog.set_level(logging.INFO, logger="piper_robot.publish")
    b._sample_can_errors()                                      # 기준점
    for i in range(1, 40):                                      # 10초마다 +3, 6.5분
        counter["bus_error"] += 3; clock["t"] += 10.0
        b._sample_can_errors()
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 2, [r.getMessage()[:40] for r in warns]     # 처음 + 5분 요약 하나
    # 요약은 첫 경고로부터 300초가 지난 첫 표본(i=31)에 나온다 — 그때까지 누적 3×31
    assert "늘었습니다" in warns[0].getMessage() and "계속 늘고" in warns[1].getMessage() and "+93" in warns[1].getMessage()
    clock["t"] += 10.0; b._sample_can_errors()                  # 안 늘었다 → 멈춤 한 번
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "멈췄습니다" in r.getMessage()]
    assert len(infos) == 1 and b._err_since == 0.0 and b._err_accum == {}
