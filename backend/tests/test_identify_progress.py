"""마스터/슬레이브 판별의 **진행 표시** (feature 문서: robots 페이지).

이 절차는 몇 초씩 조용하다 — 10초 부팅 대기, 팔마다 1.5초 반응 대기. 화면에
아무 변화가 없으면 **멈춘 것과 구분이 안 된다.** 여기서 잠그는 것은 그 표시가
실제로 흘러나오는가다.
"""

import time

import pytest

pytest.importorskip("piper_robot")
from piper_robot.hub import RobotHub  # noqa: E402


class _FakeArm:
    """하드웨어 없이 절차를 태운다 — 진짜 팔은 움직이면 안 된다."""

    def __init__(self, iface, master):
        self.iface, self.connected, self._master = iface, True, master
        self.steps: list[tuple[str, float]] = []

    def probe_command_response(self, on_step=None):
        for text, remaining in (("기준 관절값 읽는 중", 0), ("이동 명령 보내는 중", 0),
                                ("반응 기다리는 중", 1.2), ("관절값 다시 읽는 중", 0),
                                ("원위치로 되돌리는 중", 0)):
            self.steps.append((text, remaining))
            if on_step:
                on_step(text, remaining)
            time.sleep(0.02)
        return {"ok": True, "is_master": self._master,
                "moved_raw": 5 if self._master else 4500,
                "commanded_raw": 4600, "joint": "joint6"}


def _run(wait_s=0.3):
    hub = RobotHub()
    hub.arms = {"can0": _FakeArm("can0", True), "can1": _FakeArm("can1", False)}
    hub.IDENTIFY_BOOT_WAIT_S = wait_s
    assert hub.start_identify("t", ["can0", "can1"])
    seen = []
    for _ in range(400):
        st = hub.motion_status("t")
        tag = (st.get("status"), st.get("iface"), st.get("phase"), st.get("remaining"))
        if not seen or seen[-1] != tag:
            seen.append(tag)
        if st.get("status") == "done":
            return hub, seen
        time.sleep(0.01)
    raise AssertionError("끝나지 않았다")


def test_the_wait_counts_down():
    """10초를 숫자 없이 보내면 사용자는 멈춘 줄 안다."""
    _, seen = _run()
    waits = [s for s in seen if s[0] == "waiting"]
    assert len(waits) >= 3, f"대기 중 갱신이 거의 없다: {waits}"
    remains = [s[3] for s in waits]
    assert remains == sorted(remains, reverse=True), f"카운트다운이 안 준다: {remains}"
    assert remains[-1] < remains[0]


def test_the_wait_says_why_it_is_waiting():
    """"그냥 느린 것"으로 보이면 다음 사람이 대기를 지운다.

    부팅 중인 팔에 CAN 이 도착하면 부팅이 깨진다 — 그게 이 대기의 이유다.
    """
    _, seen = _run()
    phase = next(s[2] for s in seen if s[0] == "waiting")
    assert "부팅" in phase


def test_each_probe_step_reaches_the_screen():
    """명령을 보내는 중인지, 반응을 기다리는 중인지가 구분돼야 한다."""
    _, seen = _run()
    phases = [s[2] for s in seen if s[0] == "probing"]
    for expected in ("이동 명령 보내는 중", "반응 기다리는 중", "원위치로 되돌리는 중"):
        assert expected in phases, f"'{expected}' 단계가 화면에 안 나온다: {phases}"


def test_the_response_wait_also_counts_down():
    """팔마다 가장 긴 침묵이 여기다."""
    _, seen = _run()
    waiting = [s for s in seen if s[0] == "probing" and s[2] == "반응 기다리는 중"]
    assert waiting and waiting[0][3] > 0, f"남은 시간이 안 온다: {waiting}"


def test_it_says_which_arm_and_how_many_are_left():
    """팔이 둘이면 지금 어느 쪽을 건드리는지가 곧 안전 정보다."""
    _, seen = _run()
    ifaces = {s[1] for s in seen if s[0] == "probing"}
    assert ifaces == {"can0", "can1"}, ifaces


def test_results_survive_to_the_end():
    """중간 상태가 결과를 덮으면 마지막에 아무것도 안 남는다."""
    hub, _ = _run()
    st = hub.motion_status("t")
    assert st["status"] == "done"
    assert st["results"]["can0"]["role"] == "master"
    assert st["results"]["can1"]["role"] == "slave"


def test_the_page_shows_the_countdown_and_the_step():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
           / "RobotsPage.tsx").read_text()
    assert "motionStatus.phase" in src, "단계를 안 보여준다"
    assert "motionStatus.remaining > 0" in src, "남은 시간을 안 보여준다"
    # 어느 팔인지는 **행 자체**가 말한다 — 표시가 그 팔의 줄에만 뜨므로
    # 문장에 iface 를 또 넣으면 같은 말을 두 번 한다.


def test_the_row_is_matched_by_what_was_actually_stored():
    """**회귀** — 진행 표시가 화면에 아예 안 나왔다.

    행을 가리는 조건은 `motionIface === arm.iface` 인데, 거기에 슬롯 이름
    (`identify`)을 넣었다. 어느 행의 iface 와도 같지 않아 버튼이 흐려지기만 했다.

    담는 값과 비교하는 값이 **같은 것**이어야 한다.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
           / "RobotsPage.tsx").read_text()
    assert "motionIface === arm.iface" in src, "행을 iface 로 안 가린다"
    assert "setMotionIface(iface)" in src, "iface 가 아닌 것을 담는다"
    assert "setMotionIface(slot)" not in src


def test_only_the_pressed_arm_is_probed():
    """⚠ **물리적으로 움직이는 동작**이다. 연결된 것을 전부 훑으면 사용자가
    누르지도 않은 팔이 움직인다."""
    import inspect

    from app.services.robot_manager import RobotManager

    src = inspect.getsource(RobotManager.start_identify)
    assert "[iface]" in src, "부른 팔 하나만 넘기지 않는다"
    assert "for a in self.arms.values()" not in src, "연결된 전부를 훑는다"

    from pathlib import Path
    page = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
            / "RobotsPage.tsx").read_text()
    assert "handleIdentify(arm.iface)" in page, "어느 팔을 눌렀는지 안 보낸다"


def test_the_screen_and_the_daemon_use_the_same_wait_wording():
    """처음 뜨는 문구와 폴링이 가져오는 문구가 다르면 시작하자마자 글자가 바뀐다."""
    from pathlib import Path

    import inspect

    from piper_robot.hub import RobotHub

    hub_src = inspect.getsource(RobotHub._identify)
    page = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
            / "RobotsPage.tsx").read_text()
    phrase = "부팅 중인 팔을 깨뜨리지 않으려고 대기"
    assert phrase in hub_src and phrase in page


# ── 판별 결과가 역할이 되는가 ────────────────────────────────────────────────

class _ArmInfo:
    def __init__(self, role="unknown", slot=None):
        self.role, self.slot, self.side = role, slot, None


def _manager(**arms):
    from app.services.robot_manager import RobotManager
    m = RobotManager()
    m.arms = arms
    return m


def test_master_becomes_leader_and_slave_becomes_follower():
    """판별해놓고 역할을 안 세우면 **사용자가 손으로 또 골라야 한다** —
    기계가 이미 아는 것을 다시 묻는 셈이다."""
    m = _manager(can0=_ArmInfo(), can1=_ArmInfo())
    m._apply_identified_roles({"can0": {"ok": True, "role": "master"},
                               "can1": {"ok": True, "role": "slave"}})
    assert m.arms["can0"].role == "leader"
    assert m.arms["can1"].role == "follower"


def test_applying_the_same_role_twice_does_not_wipe_the_slot():
    """⚠ `set_role` 은 슬롯과 side 를 무효로 만든다. 폴링마다 다시 세우면
    **이미 배정해둔 슬롯이 계속 지워진다.**"""
    m = _manager(can0=_ArmInfo(role="leader", slot="leader_1"))
    assert m._apply_identified_roles({"can0": {"ok": True, "role": "master"}}) == []
    assert m.arms["can0"].slot == "leader_1", "슬롯이 지워졌다"


def test_a_failed_probe_leaves_the_role_alone():
    """판별에 실패했는데 역할을 바꾸면 **틀린 값을 확신에 차서 적는 것**이다."""
    m = _manager(can0=_ArmInfo(role="follower"))
    m._apply_identified_roles({"can0": {"ok": False, "error": "관절값을 읽지 못했습니다"}})
    assert m.arms["can0"].role == "follower"


def test_roles_are_applied_when_the_run_finishes():
    """데몬은 마스터인지까지만 안다 — 역할로 옮기는 것은 게이트웨이 몫이다."""
    import inspect

    from app.services.robot_manager import RobotManager

    src = inspect.getsource(RobotManager.get_motion_status)
    assert '_apply_identified_roles' in src
    assert 'st.get("status") == "done"' in src
