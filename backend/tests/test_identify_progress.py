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
    assert "motionStatus.iface" in src, "어느 팔인지 안 보여준다"
