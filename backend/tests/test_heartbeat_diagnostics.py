"""heartbeat 도착 간격 계측.

estopd 가 2.1s 공백을 보고 녹화를 세 번 죽였는데, 게이트웨이는 그 부하에서
0.3s 넘게 걸린 적이 없었다. 늦은 곳이 어디인지 가르려고 넣은 계측이다.

⚠ heartbeat 는 **안전 경로**다. 진단 때문에 이 요청이 실패하면 그 순간 팔이
선다 — 그래서 클라이언트가 보내는 값은 전부 선택이고, 없어도 통과해야 한다.
"""

from pathlib import Path

import pytest

_SVC = Path(__file__).resolve().parents[1] / "app" / "services"
_FRONT = Path(__file__).resolve().parents[2] / "frontend" / "src"


@pytest.fixture
def bridge(monkeypatch):
    from app.services.estop_bridge import EstopBridge

    b = EstopBridge()
    b._bus = None            # 버스 없이도 계측은 돈다
    return b


def test_a_heartbeat_without_a_body_still_works():
    """⚠ 진단 필드가 필수가 되면 **422 하나가 팔을 세운다.**

    본문 없는 요청도 그대로 통과해야 한다.
    """
    from app.routers.estop import HeartbeatInfo

    info = HeartbeatInfo()
    assert info.gap is None and info.hidden is None and info.rtt is None


def test_a_normal_gap_is_not_logged(bridge, caplog):
    """500ms 간격은 정상이다 — 매 tick 을 남기면 로그가 쓸모없어진다."""
    import time as _t

    with caplog.at_level("WARNING"):
        bridge.heartbeat()
        bridge.heartbeat()
    assert "heartbeat 간격" not in caplog.text


def test_a_widening_gap_is_logged_before_it_trips(bridge, caplog, monkeypatch):
    """⚠ 경고 문턱은 estopd 한도(2.0s)보다 **낮아야** 한다.

    실측 실패가 2.1s 였다. 2.0s 로 잡으면 이미 팔이 선 뒤에만 찍혀서,
    "터지기 전에 벌어지고 있었나"를 영영 못 본다.
    """
    from app.services import estop_bridge as mod

    assert mod.EstopBridge.GAP_WARN_S < 2.0, "estopd 한도와 같거나 높다 — 늦다"

    t = [100.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: t[0])
    with caplog.at_level("WARNING"):
        bridge.heartbeat()
        t[0] += 1.5
        bridge.heartbeat({"gap": 1480, "rtt": 940, "hidden": True})

    assert "heartbeat 간격 1.50s" in caplog.text
    # 세 값이 같이 남아야 어디서 벌어졌는지 갈린다
    assert "1480" in caplog.text and "940" in caplog.text and "hidden=True" in caplog.text


def test_the_browser_reports_its_own_gap_and_visibility():
    """서버 간격만 보면 **브라우저 정체와 전송 지연을 못 가른다.**"""
    src = (_FRONT / "components" / "EStopButton.tsx").read_text()
    assert "performance.now()" in src, "브라우저가 자기 간격을 안 잰다"
    assert "document.hidden" in src, "탭 백그라운드 여부를 안 보낸다"
    assert "'/estop/heartbeat', { gap, hidden: document.hidden, rtt, seq }" in src.replace('"', "'"), \
        "정황을 heartbeat 와 같이 안 보낸다"


def test_the_browser_times_its_own_request():
    """⚠ 타이머 간격만으로는 **브라우저 안에서 대기한 시간**을 못 본다.

    실측: 타이머는 488ms 로 정시인데 서버가 본 간격은 1.13s 였다. 게이트웨이
    이벤트 루프도(p95 2ms) vite 프록시도(5ms) 멀쩡했다. 그렇다면 요청이 나가기
    전에 어딘가 걸려 있었다는 뜻이고, 그걸 보려면 왕복 시간을 재야 한다.
    """
    src = (_FRONT / "components" / "EStopButton.tsx").read_text()
    assert "rtt = Math.round(performance.now() - sent)" in src, "왕복 시간을 안 잰다"
    assert "finally" in src, "요청이 실패하면 왕복 시간이 안 갱신된다"


def test_a_missing_beat_is_told_apart_from_a_late_one(bridge, caplog, monkeypatch):
    """⚠ 이게 남은 갈림길이다.

    지금까지 실측으로 지운 것: 브라우저 타이머(정시 501ms), 게이트웨이
    이벤트 루프(p95 2ms), vite 프록시(5ms), API 클라이언트(맨 fetch, 큐 없음).
    그런데도 서버가 본 **도착** 간격은 2.16s 였다.

    번호가 이어지면 요청이 늦게 온 것이고, 번호가 건너뛰면 **아예 안 간** 것이다.
    고쳐야 할 곳이 서로 다르므로 로그에서 갈려 있어야 한다.
    """
    from app.services import estop_bridge as mod

    t = [100.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: t[0])

    with caplog.at_level("WARNING"):
        bridge.heartbeat({"seq": 10, "gap": 500})
        t[0] += 2.2
        bridge.heartbeat({"seq": 14, "gap": 500})      # 11,12,13 이 안 왔다
    assert "유실 3건" in caplog.text, "빠진 번호를 안 센다"

    caplog.clear()
    with caplog.at_level("WARNING"):
        t[0] += 2.2
        bridge.heartbeat({"seq": 15, "gap": 500})      # 번호는 이어진다
    assert "유실 없음" in caplog.text, "늦게 온 것과 안 온 것을 안 가른다"


def test_the_sequence_keeps_up_even_when_no_gap_is_logged(bridge, monkeypatch):
    """번호 추적이 경고 안에만 있으면, **정상 구간을 지난 뒤** 유실 수가
    통째로 틀린다 — 갭이 날 때만 갱신되기 때문이다."""
    from app.services import estop_bridge as mod

    t = [100.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: t[0])
    for n in (1, 2, 3):
        t[0] += 0.5
        bridge.heartbeat({"seq": n})
    assert bridge._last_seq == 3, "조용한 구간에서 번호를 안 따라간다"


def test_a_client_without_a_sequence_does_not_break_tracking(bridge, monkeypatch):
    """예전 번들을 띄운 탭이 섞여 있어도 죽지 않아야 한다 — 안전 경로다."""
    from app.services import estop_bridge as mod

    t = [100.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: t[0])
    bridge.heartbeat({"seq": 7})
    t[0] += 0.5
    bridge.heartbeat(None)          # 구버전 탭
    assert bridge._last_seq == 7, "번호 없는 요청이 추적을 지운다"
