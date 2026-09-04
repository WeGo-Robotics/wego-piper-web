"""`lost()` 판정이 재연결 뒤에도 남는 문제.

⚠ 실기에서 겪었다(2026-09-03). 아침 09:26:24 에 USB 가 진짜로 빠져 robotd 가
`can1` 을 lost 로 선언했다. 저녁에 다시 꽂고 연결해서 **발행은 100Hz 로 되살아났는데**
`lost()` 는 11시간 내내 그 팔을 사라진 것으로 보고했다:

    lost = [{'id': 'can1', 'at': 1788395184.96},   # 09:26:24
            {'id': 'can3', 'at': 1788395503.76}]   # 09:31:43
    세그먼트 나이 = 0.01초 (= 지금 발행 중)

화면에는 "USB 연결을 확인하세요 — 뽑혔거나 컨트롤러가 내려갔을 수 있습니다" 가
16초마다 떴고(`lost` RPC 가 가끔 타임아웃 → 경보 해소 → 다음 틱에 재발), 그 경보가
`device_watch._apply_to_managers()` 를 통해 **멀쩡한 팔의 `connected` 까지 내렸다.**

원인은 `lost_at` 을 지우는 곳이 `stop()` 하나뿐이었다는 것이다.
"""

import pytest

pytest.importorskip("piper_robot")
from piper_robot import publish  # noqa: E402

LOST_AT = 1788395184.9555175  # 실기에서 읽은 값 그대로


class _NoThread:
    """스레드를 진짜로 띄우지 않는다 — 판정만 보는 시험이다."""

    def __init__(self, *a, **kw):
        pass

    def start(self):
        pass

    def is_alive(self):
        return False


def _idle_bridge(iface="can_test"):
    b = publish.ArmBridge.__new__(publish.ArmBridge)
    b.iface, b._running, b._threads = iface, False, []
    b.lost_at = LOST_AT
    return b


def test_starting_the_bridge_clears_the_lost_verdict(monkeypatch):
    """발행을 다시 여는 것이 "팔이 돌아왔다"의 결정적 증거다."""
    monkeypatch.setattr(publish, "StateWriter", lambda iface: object())
    monkeypatch.setattr(publish.threading, "Thread", _NoThread)

    b = _idle_bridge()
    b.start()

    assert b.lost_at == 0.0, "재연결했는데 아침의 판정이 그대로 남았다"


def test_the_manager_reuses_the_bridge_object():
    """**왜 지워야 하는지가 여기 있다.**

    재연결이 새 객체를 만든다면 판정은 저절로 사라진다. 그런데 매니저는
    `bridges[iface]` 를 재사용하므로 남는다 — 지우는 것은 `start()` 의 몫이다.
    """
    import inspect

    src = inspect.getsource(publish.ArmBridgeManager.start)
    assert "self.bridges.get" in src and "if b is None:" in src


def test_a_bridge_that_never_died_is_not_reported():
    """`lost()` 는 판정이 **선** 것만 내놓는다 — 0 은 목록에 없어야 한다."""
    m = publish.ArmBridgeManager.__new__(publish.ArmBridgeManager)
    m.bridges = {"can0": _idle_bridge("can0"), "can1": _idle_bridge("can1")}
    m.bridges["can0"].lost_at = 0.0

    assert [i["id"] for i in m.lost()] == ["can1"]
