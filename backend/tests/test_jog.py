"""웹 조그 (feature/manual-control.md §2 · teleoperation §3-B).

**새 안전 코드가 없다.** 필터는 CAN 을 쥔 robotd 에 살고 이 세션은 그 앞에 목표를
놓을 뿐이다. 그래서 여기서 잠그는 것은 *안전 계산*이 아니라, **그 필터에 닿기
전에 틀릴 수 있는 것들**이다: 남의 명령 경로를 덮는 것, 안 온 관절을 0 으로
채우는 것, 마스터에게 조용히 보내는 것.
"""

import inspect

import pytest

pytest.importorskip("piper_shm")
from app.services.jog import JogError, JogSession  # noqa: E402

FULL = {f"joint{i}": 0.0 for i in range(1, 7)} | {"gripper": 0.0}


class _FakeWriter:
    def __init__(self, iface, deadman_ms=0):
        self.iface, self.published, self.closed = iface, [], False
    def publish(self, values):
        from piper_shm.arm import JOINTS
        missing = [j for j in JOINTS if j not in values]
        if missing:
            raise ValueError(f"빠진 관절: {missing}")
        self.published.append(dict(values))
        return len(self.published)
    def close(self):
        self.closed = True


@pytest.fixture
def session(monkeypatch):
    from piper_shm import arm as shm_arm

    monkeypatch.setattr(shm_arm, "ActionWriter", _FakeWriter)
    monkeypatch.setattr(shm_arm, "list_segments", lambda: [])
    monkeypatch.setattr(shm_arm, "unlink", lambda name: True)
    from app.services import teleop
    teleop.teleop_session.stop()
    s = JogSession()
    yield s
    s.stop()


def test_it_refuses_to_take_over_someone_elses_command_path(session, monkeypatch):
    """⚠ **회귀 방지** — `ActionWriter` 는 `O_CREAT` 라 기존 세그먼트를 조용히 덮는다.

    추론 프록시가 조종 중인데 그 위에 열면 팔의 명령 경로를 가로채는 셈이다.
    "세그먼트 존재 = 조종 중"은 관례지 강제가 아니므로 여기서 확인해야 한다.
    """
    from piper_shm import arm as shm_arm

    monkeypatch.setattr(shm_arm, "list_segments",
                        lambda: [shm_arm.segment_name("can1", shm_arm.KIND_ACTION)])
    with pytest.raises(JogError, match="쥐고 있습니다"):
        session.start("can1", FULL)


def test_the_first_goal_is_the_current_pose(session):
    """0 으로 채우면 정규화 좌표의 "가운데"라 그럴듯해 보이는데,
    그게 첫 명령이 되면 **팔이 튄다.**"""
    pose = dict(FULL, joint2=-40.0, gripper=12.0)
    session.start("can1", pose)
    assert session._goal == pose


def test_a_partial_goal_is_merged_not_zero_filled(session):
    """`publish` 는 전 관절을 요구한다 — 안 온 관절을 0 으로 채우면 그게 명령이 된다."""
    session.start("can1", dict(FULL, joint2=-40.0))
    goal = session.set_goal({"joint1": 15.0})
    assert goal["joint1"] == 15.0
    assert goal["joint2"] == -40.0, "안 보낸 관절이 0 으로 덮였다"
    assert set(goal) == set(FULL)


def test_stopping_removes_the_segment(session):
    """남겨두면 발행자 없는 세그먼트가 되어 장치 감시가 "발행이 멈췄다"로 읽는다."""
    unlinked = []
    from piper_shm import arm as shm_arm
    shm_arm.unlink = lambda name: unlinked.append(name) or True

    session.start("can1", FULL)
    writer = session._writer
    session.stop()
    assert writer.closed and unlinked, "닫기만 하고 세그먼트를 안 지운다"
    assert not session.is_running


def test_two_sessions_cannot_run_at_once(session):
    session.start("can1", FULL)
    with pytest.raises(JogError):
        session.start("can0", FULL)


def test_it_marks_the_teleop_activity(session):
    """표가 거짓말하면 추론이 조그 중에도 시작된다."""
    from app.services import exclusivity as ex

    session.start("can1", FULL)
    assert ex.is_running(ex.Activity.TELEOP)
    assert ex.Activity.TELEOP in ex.blocking(ex.Activity.INFERENCE)
    session.stop()
    assert not ex.is_running(ex.Activity.TELEOP)


def test_the_goal_is_republished_faster_than_the_deadman():
    """목표 한 번에 팔이 1초쯤 움직인다. 그동안 조용하면 데드맨이 중간에 팔을 세워
    슬라이더가 뚝뚝 끊긴다."""
    from app.services import jog

    assert 1000.0 / jog.REPUBLISH_HZ < jog.DEADMAN_MS, \
        "재발행이 데드맨보다 느리다 — 조그 중에 팔이 선다"


def test_an_idle_session_closes_itself():
    """열어둔 채 잊어버리면 추론·녹화가 계속 막힌다."""
    from app.services import jog

    assert jog.IDLE_TIMEOUT_S > 0
    assert "self.stop()" in inspect.getsource(JogSession._republish)


# ── 역할 가드 ────────────────────────────────────────────────────────────────

def test_a_master_arm_is_refused_with_a_reason():
    """⚠ 마스터에게 보낸 명령은 **에러도 없이 사라진다** — 사용자는 팔이 고장 났다고
    생각하게 된다. 조용히 보내느니 막고 이유를 말한다."""
    from app.routers import robots

    src = inspect.getsource(robots._require_commandable)
    assert 'arm.role == "leader"' in src and "무시" in src
    assert 'arm.role != "follower"' in src and "찾기" in src, \
        "역할을 모를 때도 막아야 한다 — 마스터일 수 있다"


def test_jog_start_goes_through_both_guards():
    """배타(같은 팔을 둘이 밀지 않기)와 역할(마스터가 아니기) 둘 다 필요하다."""
    from app.routers import robots

    src = inspect.getsource(robots.jog_start)
    assert "require_idle(Activity.TELEOP)" in src
    assert "_require_commandable" in src


# ── 화면 ────────────────────────────────────────────────────────────────────

def _src(rel: str) -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / "frontend" / "src" / rel).read_text()


def test_the_slider_has_no_built_in_destination():
    """⚠ 어디로 보내는지가 이 컴포넌트에서 **가장 중요한 사실**이다.

    기본 목적지가 있으면 호출부가 그걸 안 보고 지나간다 — 추론 경로로 보내는 줄
    모르고 조그에 쓰거나, 그 반대가 된다.
    """
    from conftest import code_only

    # ⚠ 주석을 걷어내고 본다 — **왜 안 박아뒀는지** 적어둔 설명이 이 검사에 걸리면
    #   안 된다 (이 저장소에서 세 번째다: 9999px, window.confirm, 그리고 이것).
    src = code_only(_src("components/ManualControlPanel.tsx"))
    assert "manual-action" not in src, "목적지가 박혀 있다"
    assert "onSend" in src and "onSend?" not in src, "목적지가 선택 사항이면 기본이 생긴다"


def test_both_call_sites_say_where_they_send():
    src = _src("pages/InferencePage.tsx")
    assert "params/manual-action" in src, "추론 경로가 사라졌다"
    jog = _src("components/JogPanel.tsx")
    assert "robots/jog/goal" in jog


def test_leaving_the_page_closes_the_session():
    """⚠ 열린 채로 두면 추론·녹화가 계속 막히고, **왜 막히는지 알 길이 없다.**"""
    src = _src("components/JogPanel.tsx")
    cleanup = src.split("useEffect(() => () =>", 1)[1][:200]
    assert "jog/stop" in cleanup, "언마운트에서 안 닫는다"


def test_the_sliders_start_from_the_arms_pose():
    """0 에서 시작하면 첫 조작이 **정규화 좌표의 가운데로 가는 큰 이동**이 된다."""
    src = _src("components/JogPanel.tsx")
    assert "parking/joints" in src, "현재 자세를 안 읽는다"
    assert "if (!runningRef.current) read()" in src, \
        "조그 중에도 읽는다 — 슬라이더와 팔이 서로를 밀어 떨린다"


def test_a_master_arm_is_refused_on_screen_too():
    """백엔드가 막아도 화면이 버튼을 주면 사용자는 눌러보고 나서야 안다."""
    src = _src("pages/RobotsPage.tsx")
    assert "arm.role === 'follower'" in src, "아무 팔에나 조그를 준다"
    assert "마스터(리더)는 외부 명령을 무시합니다" in src, "막기만 하고 이유가 없다"
