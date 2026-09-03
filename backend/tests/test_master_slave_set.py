"""마스터/슬레이브 설정 — **보내고 믿지 않는다.**

⚠ 2026-09-03 실기: robotd 로그에 `MasterSlaveConfig send failed:
SEND_MESSAGE_FAILED` 가 아홉 번 찍혔는데 **화면에는 전부 성공으로 보였다.**
SDK 의 `MasterSlaveConfig` 는 전송이 실패해도 예외를 던지지 않고 로그만 남기는데,
우리 코드는 `try/except` 로 감싸 놓고 무조건 `True, "OK"` 를 돌려줬다.

영점 굽기와 같은 실패 모양이다 — 장치가 안 했는데 화면은 했다고 한다.
"""

import inspect
import textwrap
import threading

import pytest
from conftest import python_code_only
from piper_robot.arm import Arm


class FakePiper:
    """`obeys=False` 면 명령을 받고도 모드가 안 바뀐다 — 프레임이 떨어진 경우."""

    def __init__(self, *, obeys: bool, obey_at: int = 0):
        self.sent, self._obeys, self._obey_at = 0, obeys, obey_at
        self.is_master = False

    def MasterSlaveConfig(self, linkage, *a):
        self.sent += 1
        if self._obeys and self.sent > self._obey_at:
            self.is_master = (linkage == 0xFA)


def arm_with(piper, **kw) -> Arm:
    a = Arm.__new__(Arm)
    a.iface, a._piper, a._lock = "can0", piper, threading.Lock()
    a.is_master, a.ctrl_mode = None, ""
    # 판별은 버스를 듣는 일이라 여기서는 가짜 팔이 답한다
    a.refresh_mode = lambda classify=False: setattr(a, "is_master", piper.is_master)
    return a


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("piper_robot.arm.time.sleep", lambda *_: None)


def test_a_command_the_arm_ignored_is_a_failure():
    """⚠ 이게 실기에서 놓친 것이다 — 프레임이 떨어졌는데 성공이라 답했다."""
    piper = FakePiper(obeys=False)
    ok, msg = arm_with(piper).set_master_slave(True)
    assert ok is False, msg
    assert "바뀌지 않았습니다" in msg, msg
    assert piper.sent == Arm.MS_ATTEMPTS, "재시도를 안 한다"


def test_a_dropped_frame_is_retried_not_given_up_on():
    """⚠ 한 번 떨어졌다고 포기하면, 사람은 눌러도 안 되는 버튼을 계속 누른다.
    실기 로그의 실패가 정확히 이 모양이었다."""
    piper = FakePiper(obeys=True, obey_at=1)     # 첫 번은 먹지 않는다
    ok, _ = arm_with(piper).set_master_slave(True)
    assert ok is True
    assert piper.sent == 2, piper.sent


def test_success_stops_retrying():
    piper = FakePiper(obeys=True)
    ok, _ = arm_with(piper).set_master_slave(True)
    assert ok is True and piper.sent == 1


def test_slave_is_verified_too():
    piper = FakePiper(obeys=True)
    piper.is_master = True
    ok, _ = arm_with(piper).set_master_slave(False)
    assert ok is True and piper.is_master is False


def test_the_result_is_checked_not_assumed():
    """⚠ SDK 의 `MasterSlaveConfig` 는 전송 실패에 **예외를 안 던진다** — 로그만
    남긴다. 그래서 `try/except` 만으로는 아무것도 못 잡는다. 팔이 실제로 그
    모드가 됐는지 봐야 한다."""
    src = python_code_only(textwrap.dedent(inspect.getsource(Arm.set_master_slave)))
    assert "self.is_master is master" in src, "결과를 안 본다"


def test_a_preset_does_not_call_a_failed_mode_applied():
    """⚠ `load_preset` 은 "무엇이 실제로 적용됐는지 돌려준다" 고 약속한다.
    모드가 안 먹었는데 `applied` 에만 올리면, 화면은 leader 인데 팔은 슬레이브로
    남고 텔레옵을 걸어야 그제서야 드러난다."""
    from app.services.robot_manager import RobotManager

    src = python_code_only(textwrap.dedent(inspect.getsource(RobotManager.load_preset)))
    assert "mode_failed" in src, "모드 설정 실패를 안 돌려준다"
    assert "if not self.apply_role_mode(arm)" in src, "결과를 안 본다"
