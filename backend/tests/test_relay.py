"""리더 → 팔로워 릴레이 (feature/teleoperation.md §3-A).

**robotd 변경 0줄이다.** 리더 관절은 이미 shm 에 있고 팔로워 명령 경로도 이미
있다 — 이 루프는 둘 사이에 앉을 뿐이다. 그래서 여기서 잠그는 것은 *제어*가
아니라 **읽는 값을 믿어도 되는가**다.
"""

import inspect
import time

import pytest

pytest.importorskip("piper_shm")
from app.services.relay import RelayError, RelaySession  # noqa: E402

FULL = {f"joint{i}": 0.0 for i in range(1, 7)} | {"gripper": 0.0}


class _FakeWriter:
    def __init__(self, iface, deadman_ms=0):
        self.published, self.closed = [], False
    def publish(self, values):
        self.published.append(dict(values)); return len(self.published)
    def close(self):
        self.closed = True


class _FakeReader:
    """리더의 shm 상태. `age` 로 얼마나 낡았는지 흉내낸다."""
    def __init__(self, iface, values=None, age=0.0, empty=False):
        self.values, self.age, self.empty, self.closed = values or FULL, age, empty, False
    def read(self):
        if self.empty:
            return None
        return {"values": dict(self.values),
                "can_wall_ns": time.time_ns() - int(self.age * 1e9)}
    def close(self):
        self.closed = True


@pytest.fixture
def wired(monkeypatch):
    from piper_shm import arm as shm_arm
    from app.services import teleop

    monkeypatch.setattr(shm_arm, "ActionWriter", _FakeWriter)
    monkeypatch.setattr(shm_arm, "list_segments", lambda: [])
    monkeypatch.setattr(shm_arm, "unlink", lambda name: True)
    # ⚠ CAN 상태는 **conftest 의 `healthy_can`** 이 막는다. 여기서
    #   `teleop.require_healthy_bus` 를 패치하는 건 효과가 없다 — jog·relay 가
    #   그 이름을 `from ... import` 로 미리 묶기 때문이다. 실제로 그렇게 적혀
    #   있었고 아무도 안 먹는 줄 몰랐다.
    from app.services import teleop
    monkeypatch.setattr(teleop, "enable_torque", lambda iface: None)
    teleop.teleop_session.stop()

    def _make(**kw):
        monkeypatch.setattr(shm_arm, "StateReader",
                            lambda iface: _FakeReader(iface, **kw))
        return RelaySession()
    yield _make


def test_it_refuses_a_leader_that_publishes_nothing(wired):
    """⚠ 발행 없는 리더로 열면 릴레이는 **조용히 아무것도 안 한다** —
    사용자는 팔이 고장난 줄 안다. 시작 전에 한 번 읽어 본다."""
    s = wired(empty=True)
    with pytest.raises(RelayError, match="발행"):
        s.start("can0", "can1")


def test_the_same_arm_cannot_be_both_ends(wired):
    s = wired()
    with pytest.raises(RelayError, match="같은 팔"):
        s.start("can0", "can0")


def test_it_relays_the_leader_pose(wired):
    s = wired(values=dict(FULL, joint2=-31.0))
    s.start("can0", "can1")
    time.sleep(0.25)
    sent = s._writer.published
    s.stop()
    assert sent, "아무것도 안 보냈다"
    assert sent[-1]["joint2"] == -31.0


def test_a_frozen_leader_stops_the_relay(wired):
    """⚠ 얼어붙은 자세를 계속 밀면 팔로워는 그게 **사람의 의도**인 줄 안다.

    안 보내면 robotd 의 데드맨이 팔을 그 자리에 세운다 — 그쪽이 정직하다.
    """
    s = wired(age=5.0)
    s.start("can0", "can1")
    time.sleep(0.25)
    published = list(s._writer.published)
    status = s.status()
    s.stop()
    assert not published, "낡은 자세를 계속 보냈다"
    assert status["stale"]


def test_stopping_releases_both_ends(wired):
    s = wired()
    s.start("can0", "can1")
    writer, reader = s._writer, s._reader
    s.stop()
    assert writer.closed and reader.closed, "자원이 남는다"
    assert not s.is_running

    from app.services import exclusivity as ex
    assert not ex.is_running(ex.Activity.TELEOP)


def test_it_marks_the_teleop_activity(wired):
    from app.services import exclusivity as ex

    s = wired()
    s.start("can0", "can1")
    assert ex.is_running(ex.Activity.TELEOP)
    assert ex.Activity.TELEOP in ex.blocking(ex.Activity.RECORDING)
    s.stop()


def test_nothing_is_ever_sent_to_the_leader():
    """마스터는 외부 명령을 무시한다 — 보낼 이유가 없고, 보내면 모드가 흔들릴 수 있다."""
    src = inspect.getsource(RelaySession)
    body = src.split("def _loop", 1)[1]
    assert "open_action_writer" not in body, "루프가 리더 쪽에 라이터를 연다"
    # 라이터는 팔로워에게만 열린다
    start = src.split("def start", 1)[1].split("def stop", 1)[0]
    assert "open_action_writer(follower" in start


def test_the_relay_is_faster_than_the_deadman():
    """느리면 조종 중에 팔이 선다."""
    from app.services import relay

    assert 1000.0 / relay.RELAY_HZ < relay.DEADMAN_MS


def test_estop_closes_whichever_session_runs():
    """조그와 릴레이가 각각 자기 자원을 들고 있다 — 세션 플래그만 닫으면 남는다."""
    from app.services import exclusivity as ex

    src = inspect.getsource(ex._stop_teleop)
    assert "jog_session" in src and "relay_session" in src
    assert "disable_torque" not in src, "게이트웨이가 토크를 끊으려 한다"


def test_the_command_takeover_lives_in_one_place():
    """조그와 릴레이가 **같은 위험한 일**을 한다 — 두 벌이면 한쪽만 고치게 된다."""
    from app.services import jog, relay

    for mod in (jog, relay):
        src = inspect.getsource(mod)
        assert "open_action_writer" in src
        assert "list_segments" not in src, f"{mod.__name__} 이 점유 확인을 따로 한다"


def test_jog_and_relay_cannot_be_started_together_on_screen():
    """둘이 **같은 명령 경로**를 쓴다. 백엔드가 막지만, 버튼이 살아 있으면
    사용자는 눌러보고 나서야 안다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components"
           / "JogPanel.tsx").read_text()
    # 조건 자체가 아니라 **릴레이가 조그 버튼을 막는가**를 본다. 조건이 늘어날
    # 수 있다 — 지금은 서버가 알려준 `blocked` 도 함께 막는다(다른 카드나 새로고침
    # 뒤에는 로컬 `relaying` 이 비어 있어서 그것만으로는 모자란다).
    start_btn = src.split("running ? stop : start", 1)[1].split(">", 1)[0]
    assert "relaying" in start_btn, "릴레이 중에도 조그 버튼이 살아 있다"
    assert "disabled=" in start_btn
    assert "disabled={busy || running}" in src, "조그 중에도 릴레이 버튼이 살아 있다"


def test_leaving_the_page_closes_the_relay_too():
    """조그만 닫고 릴레이를 두면 팔이 계속 리더를 따라간다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components"
           / "JogPanel.tsx").read_text()
    cleanup = src.split("useEffect(() => () => {", 1)[1][:300]
    assert "relay/stop" in cleanup and "jog/stop" in cleanup


def _page() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
            / "RobotsPage.tsx").read_text()


def test_registered_arms_get_the_controls_too():
    """**회귀 — 화면에 통째로 안 보였다.**

    조작 패널을 `connectedArms` 에만 붙였는데 그 목록은 `!ready` 다 — **등록하는
    순간 빠진다.** 등록된 팔은 `readyArms` 로 그려지므로, 실제로 쓰는 상태에서는
    패널이 어디에도 없었다.
    """
    src = _page()
    ready_block = src.split("readyArms.map", 1)[1]
    assert "JogPanel" in ready_block, "등록된 팔에 조작 패널이 없다"


def test_the_leader_is_looked_up_across_all_arms():
    """`connectedArms` 에서 찾으면 **등록된 팔끼리는 릴레이 버튼이 영영 안 뜬다.**"""
    src = _page()
    assert "arms.find((a) => a.role === 'leader'" in src
    assert "leader={connectedArms.find" not in src


def test_the_leader_must_be_on_the_same_side():
    """⚠ 왼팔을 오른쪽 리더로 끌면 조작자의 손 방향과 팔 방향이 뒤집힌다 —
    사람이 실수하는 자리다. 예전에는 연결된 **첫** 리더를 아무 팔에나 넘겼다."""
    src = _page()
    assert "a.side === side" in src, "같은 쪽을 안 본다"
    assert src.count("leader={leaderFor(arm.side)}") == 2, "두 목록이 같은 규칙을 써야 한다"


def test_the_side_is_passed_so_the_panel_can_explain():
    """좌/우 미지정 팔은 짝을 정할 수 없다 — 화면이 그 이유를 말해야 한다."""
    assert _page().count("side={arm.side}") == 2
