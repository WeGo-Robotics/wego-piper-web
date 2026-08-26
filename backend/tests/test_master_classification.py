"""마스터/슬레이브 판별은 **어떤 프레임이 오는지**로 한다.

수집 중에 리더가 슬레이브로 표시된다는 신고에서 나왔다. 로그를 보니 모드를
바꾸는 호출은 하나도 없었다 — 팔이 아니라 **판정**이 뒤집힌 것이다.

예전 규칙은 RX **개수**만 셌다: 0.35초 동안 안 늘면 마스터. 그런데 마스터는
사람이 팔을 움직이는 동안 제어지령(0x15x)을 송신한다 — 그게 마스터의 정의다.
그 프레임이 호스트 RX 에 잡히므로 조작하는 내내 슬레이브로 읽혔다.
"""

import sys
from pathlib import Path

import pytest

_ROBOT = Path(__file__).resolve().parents[2] / "robot"
sys.path.insert(0, str(_ROBOT))


def _arm(monkeypatch, groups=None, error=None):
    from piper_robot import arm as arm_mod

    a = arm_mod.Arm.__new__(arm_mod.Arm)
    a.iface = "can9"
    a.is_master = None
    seen = {"error": error} if error else {
        "groups": {"slave_fb": 0, "master_fb": 0, "master_ctrl": 0,
                   "driver": 0, "other": 0, **(groups or {})}}
    monkeypatch.setattr(arm_mod, "sniff_can_ids", lambda iface, duration: seen)
    return a


def test_a_moving_master_is_still_a_master(monkeypatch):
    """⚠ **회귀** — 이게 수집 중에 라벨이 뒤집히던 그 상황이다.

    사람이 리더를 끄는 동안 0x15x 가 쏟아진다. 개수를 세면 '트래픽 있음' 이고
    옛 규칙은 그걸 슬레이브로 읽었다.
    """
    a = _arm(monkeypatch, {"master_ctrl": 400})
    a._classify_master()
    assert a.is_master is True, "조작 중인 마스터를 슬레이브로 읽는다"


def test_an_idle_master_is_a_master(monkeypatch):
    """가만히 있는 마스터는 아무것도 안 보낸다."""
    a = _arm(monkeypatch)
    a._classify_master()
    assert a.is_master is True


def test_a_slave_is_recognised_by_its_periodic_feedback(monkeypatch):
    """슬레이브는 **조건 없이** 0x2Ax 를 계속 보낸다 — 가장 믿을 만한 신호다."""
    a = _arm(monkeypatch, {"slave_fb": 120})
    a._classify_master()
    assert a.is_master is False


def test_slave_feedback_wins_over_control_traffic(monkeypatch):
    """둘 다 보이면 슬레이브다.

    주기 피드백은 슬레이브만 낸다. 제어지령은 버스에 흐를 수 있는 남의 프레임일
    수도 있으므로, 판정 순서가 뒤바뀌면 슬레이브를 마스터로 읽는다.
    """
    a = _arm(monkeypatch, {"slave_fb": 100, "master_ctrl": 300})
    a._classify_master()
    assert a.is_master is False


def test_the_linkage_mode_shortcut_still_wins(monkeypatch):
    """보고해 주면 그게 제일 확실하다 — 다만 실측하면 마스터가 `Standby` 를 낸다."""
    a = _arm(monkeypatch, {"slave_fb": 999})
    a._classify_master(mode_int=0x06)
    assert a.is_master is True


def test_an_unreadable_bus_yields_no_verdict(monkeypatch):
    """추측이 라벨로 굳는 것보다 '모른다' 가 낫다."""
    a = _arm(monkeypatch, error="No such device")
    a._classify_master()
    assert a.is_master is None


def test_the_classifier_no_longer_counts_packets():
    """개수를 세는 방식으로 돌아가면 같은 버그가 그대로 돌아온다."""
    src = (_ROBOT / "piper_robot" / "arm.py").read_text()
    body = src.split("def _classify_master", 1)[1].split("\n    def ", 1)[0]
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    assert "_read_can_rx" not in code, "다시 RX 개수를 센다"
    assert "sniff_can_ids" in code, "프레임 ID 를 안 본다"


# ── 언제 판별하는가 ─────────────────────────────────────────────────────────

_HUB = _ROBOT / "piper_robot" / "hub.py"
_MGR = Path(__file__).resolve().parents[1] / "app" / "services" / "robot_manager.py"


def test_polling_does_not_reclassify():
    """⚠ **마스터/슬레이브는 폴링으로 확인할 값이 아니다.**

    저절로 바뀌지 않는다 — 우리가 세우거나 전원이 끊길 때만 바뀐다. 그런데
    `/robots/current` 가 1초마다 불리면서 매번 다시 판별했고, 판별은 버스를
    0.35초 듣는 일이라 팔 2대면 **매 초의 0.7초**를 여기에 썼다.

    비싸기만 한 게 아니라, 판별이 틀릴 창이 폴링 횟수만큼 열린다.
    """
    src = _MGR.read_text()
    body = src.split("def get_current", 1)[1].split("\n    def ", 1)[0]
    assert "refresh_mode()" in body, "ctrl_mode 갱신까지 빠졌다"
    assert "classify" not in body, "폴링이 아직 판별한다"


def test_connecting_does_classify():
    """연결이 판별하는 자리다 — 그 팔이 무엇인지 그때 정해진다."""
    src = _MGR.read_text()
    body = src.split("def connect(", 1)[1].split("\n    def ", 1)[0]
    assert '"refresh_mode", self.iface, True' in body, "연결할 때 판별을 안 한다"


def test_setting_the_mode_reclassifies():
    """방금 바꾼 값은 확인해야 한다 — 안 그러면 실패해도 성공으로 보인다."""
    for path, marker in ((_ROBOT / "piper_robot" / "arm.py", "def set_master_slave"),
                         (_MGR, "def set_master_slave")):
        body = path.read_text().split(marker, 1)[1].split("\n    def ", 1)[0]
        assert "classify=True" in body, f"{path.name}: 세운 뒤 확인을 안 한다"


def test_the_daemon_defaults_to_not_classifying():
    """⚠ 기본값이 판별이면, 인자를 안 넘기는 옛 호출부가 **조용히** 비싼 경로를 탄다."""
    body = _HUB.read_text().split("def refresh_mode", 1)[1].split("\n    def ", 1)[0]
    assert "classify: bool = False" in body, "기본값이 판별이다"
