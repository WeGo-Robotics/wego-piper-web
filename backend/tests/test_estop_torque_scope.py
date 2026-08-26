"""E-stop 토크 차단이 **어느 팔에** 가나.

리더암이 자꾸 슬레이브로 돌아간다는 신고에서 나왔다. E-stop 이 쥐고 있는 팔
전부의 토크를 끊었는데, `DisablePiper()` 가 모터를 끄면서 마스터(示教输入臂)
연동 설정까지 푼다. 실기에서 재현했다:

    can1 master_slave=master  →  토크 차단  →  master_slave=slave

마스터 팔은 사람이 손으로 끄는 팔이라 **이미 토크가 없다** — 끊어서 얻는
안전이 0인데 텔레옵만 깨졌다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "robot"))


class FakeArm:
    def __init__(self, is_master):
        self.is_master = is_master
        self.cut = False

    def disable_torque(self):
        self.cut = True
        return True


def _hub(arms):
    from piper_robot.hub import RobotHub

    hub = RobotHub.__new__(RobotHub)
    hub.arms = arms
    return hub


def test_a_master_arm_keeps_its_linkage_setting():
    """⚠ **회귀** — 여기서 끊으면 리더가 슬레이브로 돌아간다."""
    arms = {"can1": FakeArm(True), "can0": FakeArm(False)}
    done = _hub(arms).disable_all_torque()

    assert arms["can1"].cut is False, "마스터 팔의 토크를 끊었다"
    assert arms["can0"].cut is True, "팔로워 팔을 안 끊었다 — E-stop 이 무의미해진다"
    assert done == ["can0"]


def test_an_unclassified_arm_is_still_cut():
    """판정 불가는 안전이 아니다.

    모드를 모르는 팔은 토크가 살아 있을 수 있다 — 그러면 E-stop 이 해야 할 일이
    남아 있는 것이다. 모를 때는 끊는 쪽으로 기운다.
    """
    arms = {"can0": FakeArm(None)}
    assert _hub(arms).disable_all_torque() == ["can0"]


def test_the_skip_is_decided_by_measurement_not_by_label():
    """`role` 은 사람이 붙인 라벨이라 팔의 실제 모드와 어긋날 수 있다 —
    토크 과부하로 모드가 뒤집힌 채 화면만 leader 였던 일이 실제로 있었다.

    라벨로 건너뛰면 **토크가 살아 있는 팔**을 놓친다.
    """
    src = (Path(__file__).resolve().parents[2] / "robot" / "piper_robot" / "hub.py").read_text()
    body = src.split("def disable_all_torque", 1)[1].split("\n    def ", 1)[0]
    assert "arm.is_master is True" in body, "측정값으로 안 가른다"
    assert "arm.role" not in body, "라벨로 가르고 있다"


# 부분 실패(팔 하나가 CAN 오류)는 test_teleop_guard.py 가 이미 본다.
