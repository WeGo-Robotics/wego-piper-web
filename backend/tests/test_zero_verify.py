"""영점 굽기 — **팔의 "성공" 을 그대로 믿지 않는다.**

⚠ 2026-09-03 실기: 영점을 16번 눌러 전부 `200 OK` 를 받았는데 **하나도 안
굽혔다.** 팔이 `is_set_zero_successfully=1` 로 응답했기 때문이다. 화면에는
아무 신호도 없어서, 사람은 굽힌 줄 알고 다음 관절로 넘어갔다.

영점이 옮겨졌으면 그 관절은 0 을 보고해야 한다. 확인할 근거가 바로 옆에
있는데 안 보는 것은 성공을 지어내는 것이다.
"""

import threading

import pytest
from piper_robot.arm import Arm


class FakePiper:
    """`applied=False` 면 성공이라 응답하면서 값은 안 바꾼다 — 실기 그대로."""

    def __init__(self, raw: int, *, applied: bool, flag: int = 1):
        self._raw, self._applied, self._flag = raw, applied, flag

    def ClearRespSetInstruction(self): pass
    def GripperCtrl(self, *a): pass

    def JointConfig(self, joint_num=7, set_zero=0, **kw):
        if self._applied:
            self._raw = 0

    def GetRespInstruction(self):
        flag = self._flag
        return type("R", (), {"instruction_response":
                              type("I", (), {"is_set_zero_successfully": flag})()})()

    def GetArmJointMsgs(self):
        raw = self._raw
        return type("M", (), {"joint_state": type("J", (), {
            f"joint_{i}": raw for i in range(1, 7)})()})()


def arm_with(piper) -> Arm:
    a = Arm.__new__(Arm)
    a.iface, a._piper, a._lock = "can0", piper, threading.Lock()
    return a


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("piper_robot.arm.time.sleep", lambda *_: None)


def test_a_flash_that_did_not_take_is_reported_as_a_failure():
    """⚠ 이게 실기에서 놓친 것이다 — 팔은 성공이라 하고 값은 그대로였다."""
    out = arm_with(FakePiper(-3718, applied=False)).set_hardware_zero("joint1")
    assert out["ok"] is False, out
    assert "0 이 되지 않았습니다" in out["error"], out
    assert out["raw_before"] == -3718 and out["raw_after"] == -3718


def test_a_flash_that_took_is_still_a_success():
    """⚠ 성공을 실패라 부르는 쪽도 값이 비싸다 — 되돌릴 수 없는 조작이라
    사람이 한 번 더 굽게 만든다."""
    out = arm_with(FakePiper(4880, applied=True)).set_hardware_zero("joint1")
    assert out["ok"] is True, out
    assert out["raw_after"] == 0


def test_a_small_residual_still_counts_as_applied():
    """실측 성공 사례 중 가장 큰 잔차가 281 이었다 — 그걸 실패로 만들면
    멀쩡한 영점을 다시 굽게 된다."""
    out = arm_with(FakePiper(-281, applied=False)).set_hardware_zero("joint2")
    assert out["ok"] is True, out


def test_the_arms_own_failure_still_wins():
    """팔이 실패라 하면 값과 무관하게 실패다."""
    out = arm_with(FakePiper(0, applied=False, flag=0)).set_hardware_zero("joint1")
    assert out["ok"] is False and "실패로 응답" in out["error"], out
