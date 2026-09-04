"""웹 조그 (feature/manual-control.md §2 · teleoperation §3-B).

**새 안전 코드가 없다.** 필터는 CAN 을 쥔 robotd 에 살고 이 세션은 그 앞에 목표를
놓을 뿐이다. 그래서 여기서 잠그는 것은 *안전 계산*이 아니라, **그 필터에 닿기
전에 틀릴 수 있는 것들**이다: 남의 명령 경로를 덮는 것, 안 온 관절을 0 으로
채우는 것, 마스터에게 조용히 보내는 것.
"""

import inspect
from pathlib import Path

import pytest

from conftest import code_only

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
    # ⚠ CAN 상태는 **conftest 의 `healthy_can`** 이 막는다. 여기서
    #   `teleop.require_healthy_bus` 를 패치하는 건 효과가 없다 — jog·relay 가
    #   그 이름을 `from ... import` 로 미리 묶기 때문이다. 실제로 그렇게 적혀
    #   있었고 아무도 안 먹는 줄 몰랐다.
    from app.services import teleop
    monkeypatch.setattr(teleop, "enable_torque", lambda iface: None)
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


def test_starting_a_session_turns_torque_on():
    """**회귀 — 실기에서 걸렸다.** "명령은 가는데 안 움직인다".

    관절 명령 경로(shm → robotd)는 토크를 안 건드린다 — 추론 프록시가 자기 연결
    시점에 켜는 것을 전제로 만들어졌다. 조그·릴레이에는 그 프록시가 없다.
    """
    from app.services import jog, relay

    for mod in (jog, relay):
        assert "enable_torque(" in inspect.getsource(mod), \
            f"{mod.__name__} 이 토크를 안 켠다"


def test_failing_to_enable_torque_does_not_block_the_start():
    """이미 켜져 있을 수도 있다. 못 켰다면 안 움직이는 것으로 사용자가 알고,
    시작 자체를 거절하면 이유가 더 흐려진다."""
    from app.services.teleop import enable_torque

    src = inspect.getsource(enable_torque)
    assert "raise" not in src and "logger.warning" in src


def test_the_screen_can_turn_torque_back_on():
    """OFF 만 있으면 끈 뒤 되돌릴 길이 화면에 없다."""
    src = _src("pages/RobotsPage.tsx")
    assert "torque?enable=true" in src and "torque?enable=false" in src


def test_the_sliders_follow_the_arm_until_you_grab_them():
    """**회귀 — 화면에서 "조그가 반응이 없다"로 보였다.**

    슬라이더를 마운트 때 한 번만 초기화했다. 그 순간 관절값이 아직 안 왔으면
    (빈 배열) **전부 0 으로 굳었고**, 그 상태로 하나를 끌면 나머지 관절까지 0 을
    목표로 보내 팔이 엉뚱한 자세로 천천히 기어갔다.

    못 쓰는 동안(`disabled`)에는 팔을 따라가고, 조작 중에는 안 따라간다 —
    따라가면 슬라이더와 팔이 서로를 밀어 떨린다.
    """
    src = _src("components/ManualControlPanel.tsx")
    assert "if (!disabled || currentJoints.length === 0) return" in src, \
        "따라가지 않거나, 조작 중에도 따라간다"
    assert "[disabled, currentJoints]" in src


def test_there_is_a_way_back_to_the_parking_pose():
    """조그로 되돌리려면 관절 여섯을 손으로 맞춰야 한다 — 버튼 하나면 될 일이다."""
    src = _src("components/JogPanel.tsx")
    assert "parking/go" in src, "파킹으로 보내는 길이 없다"
    # ⚠ 확인창은 **사용자가 지웠다.** 자주 누르는 버튼이고 되돌릴 수 있는
    #   조작이라(다시 조그하면 된다) 매번 묻는 것이 성가시다는 판단이다.
    #   경고는 버튼 `title` 로 남는다. 되돌릴 수 없는 조작(하드웨어 영점)과는
    #   다르게 다룬다.
    assert "confirm(" not in code_only(src), "확인창이 다시 생겼다"
    home = src.split("onClick={goParking}", 1)[1][:260]
    assert "title=" in home, "경고가 아무 데도 없다"


def test_going_to_parking_is_blocked_while_something_else_drives():
    """조그·릴레이가 명령 경로를 쥔 채로 파킹을 보내면 둘이 팔을 두고 다툰다."""
    src = _src("components/JogPanel.tsx")
    home = src.split("onClick={goParking}", 1)[1][:200]
    assert "busy || running || relaying" in home


def test_a_dead_bus_is_reported_instead_of_a_silent_success():
    """⚠ **SDK 가 전송 실패를 예외로 안 준다.**

    팔 전원이 꺼져 있어도 `JointCtrl`·`EndPoseCtrl` 은 조용히 돌아오고 로그에만
    `SEND_MESSAGE_FAILED` 가 남는다 — 실기에서 다섯 번을 "성공"으로 보고했다.
    `tx_errors` 는 그때도 0 이었다. 결정적 신호는 **버스 상태**다.
    """
    from piper_robot.can import CAN_HEALTHY, can_unhealthy_reason

    assert CAN_HEALTHY == "ERROR-ACTIVE"
    # 모르는 인터페이스 경로는 `test_an_unknown_interface_is_not_called_bad` 가 본다 —
    # conftest 가 버스를 건강하게 고정하므로 여기서 부르면 단언이 공허해진다
    assert can_unhealthy_reason is not None


def test_sessions_refuse_to_start_on_a_dead_bus():
    """안 막으면 조그가 열리고 슬라이더도 움직이는데 **팔만 안 움직인다** —
    사용자는 소프트웨어를 의심하게 된다."""
    from app.services import jog, relay

    for mod in (jog, relay):
        assert "require_healthy_bus(" in inspect.getsource(mod), \
            f"{mod.__name__} 이 버스를 안 본다"


def test_the_end_pose_command_checks_the_bus_before_sending():
    from piper_robot.hub import RobotHub

    src = inspect.getsource(RobotHub.jog_end_pose)
    assert "can_unhealthy_reason" in src
    assert src.index("can_unhealthy_reason") < src.index("move_end_pose"), \
        "보낸 뒤에 본다 — 그러면 이미 실패한 뒤다"


def test_the_bus_is_not_checked_per_frame():
    """`ip` 호출은 3~4ms 다. 명령마다는 괜찮지만 **프레임마다는** 안 된다.

    ⚠ 금지가 아니라 **간격**이 요점이다. 오류 카운터 감시(`error_counters`)가
      같은 `ip` 를 쓰는데, 그건 10초에 한 번이라 괜찮다 — 발행 루프 안에서
      매 프레임 부르는 것만 막는다.
    """
    import re
    from pathlib import Path

    publish = (Path(__file__).resolve().parents[2] / "robot" / "piper_robot"
               / "publish.py").read_text()
    # 상태 조회는 여전히 금지 — 명령 경로에서 프레임마다 불렸던 전례가 있다.
    # ⚠ **docstring 을 떼고 본다.** 왜 그걸 안 쓰는지 설명하는 글이 그 이름을
    #   적는다 — 이 저장소에서 여섯 번째다.
    import ast

    tree = ast.parse(publish)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
              for a in n.names}
    assert "can_state" not in names and "can_unhealthy_reason" not in names

    loop = publish.split("def _publish_loop", 1)[1].split("\n    def ", 1)[0]
    for call in re.findall(r"self\.(_sample_can_errors|_declare_lost)\(", loop):
        if call != "_sample_can_errors":
            continue
        # 간격 변수로 감싸여 있어야 한다
        guard = loop.split("self._sample_can_errors(", 1)[0][-200:]
        assert "next_err" in guard and "ERR_SAMPLE_S" in guard, \
            "오류 카운터 조회가 프레임마다 돈다"


def test_the_logic_tests_do_not_read_this_machines_can_bus():
    """⚠ **회귀 — 실기에서 can1 이 내려가자 단위 테스트가 우수수 깨졌다.**

    세션 시작이 버스를 확인하게 만들면서 테스트가 하드웨어에 매였다. 로직
    테스트가 그 기계의 상태에 매이면 그때부터 아무것도 못 믿는다 — 실패가
    코드 탓인지 케이블 탓인지 갈 수 없다.

    ⚠ **이 테스트는 예전에 문자열만 봤다.** 두 파일에
    `monkeypatch.setattr(teleop, "require_healthy_bus", ...)` 가 있는지만 확인했는데,
    `jog`·`relay` 가 그 이름을 `from ... import` 로 미리 묶으므로 **그 패치는 아무
    효과가 없었다.** 문자열은 있고 보호는 없는 상태로 통과했고, 실기 `can1` 이
    나빠지자 9개가 깨져서야 드러났다. 이제 **실제로 막히는지**를 본다.
    """
    from app.services.teleop import require_healthy_bus
    from piper_robot import can

    # 이 기계에 실재하는 인터페이스 이름으로 불러도 통과해야 한다
    require_healthy_bus("can0")
    require_healthy_bus("can1")

    # 그리고 그 통과가 "우연히 건강해서"가 아니라 conftest 가 고정했기 때문이다
    assert can.can_state("아무거나") == can.CAN_HEALTHY


def test_an_unknown_interface_is_not_called_bad(monkeypatch):
    """모르는 것과 나쁜 것은 다르다. conftest 가 버스를 건강하게 고정하므로,
    이 경로를 보려면 **여기서 직접** 모르는 상태를 만들어야 한다."""
    from piper_robot import can

    monkeypatch.setattr(can, "can_state", lambda iface: None)
    assert can.can_unhealthy_reason("canX_없음") is None


def test_the_button_is_not_called_the_origin():
    """⚠ **"원점으로" 는 거짓말이었다.** 이 버튼은 엔코더 영점이 아니라 사람이
    저장해 둔 파킹 자세로 간다 — 저장이 없으면 기본 자세(J5 만 22.75° 든 영점)다.
    "0 으로 갈 줄 알았는데 도대체 어딜 가는 거냐" 로 보고됐다.

    하드웨어 영점(`JointConfig(set_zero=0xAE)`)은 되돌릴 수 없는 다른 조작이라,
    이름이 겹치면 그쪽과 헷갈린다.
    """
    src = _src("components/JogPanel.tsx")
    label = src.split("onClick={goParking}", 1)[1].split("</button>", 1)[0]
    assert "원점" not in label, "버튼 이름이 다시 '원점' 이 됐다"
    assert "파킹" in label, "버튼이 파킹이라고 말하지 않는다"
