"""마스터/슬레이브 판별 — **팔의 움직임**을 재야 한다.

⚠ 실기에서 무너졌다: 2026-09-02 17:01 부터 can0·can1·can2·can3 **네 대 모두**
`moved_raw: 0` 으로 master 판정이 났다. 리더/팔로워 쌍에서 넷 다 마스터일 수는
없다. 정지한 팔도 잡음이 수십 raw 인데 **정확히 0** 이라는 건 두 번 측정한 게
아니라 같은 캐시를 두 번 읽었다는 뜻이다.

원인: 프로브가 `read_joints_raw` 로 전후를 쟀는데, 그 함수는 지령(0x155~7)이
0 이 아니기만 하면 나이를 안 보고 그걸 돌려줬다. **얼어붙은 지령이 살아 있는
피드백을 가린다.**

아이러니가 핵심이다 — 지령 폴백이 있는 이유가 "마스터는 피드백을 안 보내
얼어붙기 때문" 인데, 판별이 가리려는 것이 정확히 그 조건이다.
"""

import inspect
import threading

import pytest
from conftest import code_only
from piper_robot.arm import Arm


class _Msg:
    def __init__(self, vals, ts):
        self.time_stamp = ts
        j = type("J", (), {f"joint_{i + 1}": v for i, v in enumerate(vals)})()
        self.joint_state = self.joint_ctrl = j


class FakePiper:
    """피드백과 지령을 **따로** 들고 있는 팔.

    `slave=True` 면 명령이 피드백에 반영된다(실제로 움직인다).
    `slave=False` (마스터)면 명령을 무시하고 피드백은 얼어붙어 있다.
    """

    def __init__(self, pose, *, slave: bool, ctrl_cache=None, ctrl_lead=0.0):
        self.slave = slave
        self._fb = list(pose)
        # ⚠ 실기에서 이게 차 있었다 — 갱신은 멈췄는데 값은 남아 있는 상태.
        self._ctrl = list(ctrl_cache) if ctrl_cache else [0] * 6
        self._ctrl_lead = ctrl_lead
        self.sent = []

    def GetArmJointMsgs(self):   return _Msg(self._fb, 100.0)
    def GetArmJointCtrl(self):   return _Msg(self._ctrl, 100.0 + self._ctrl_lead)
    def EnablePiper(self):       return True
    def ModeCtrl(self, *a):      return True

    def JointCtrl(self, *vals):
        self.sent.append(list(vals))
        if self.slave:
            self._fb = list(vals)        # 슬레이브는 따라간다


def arm_with(piper) -> Arm:
    a = Arm.__new__(Arm)
    a.iface, a._piper, a._lock = "can0", piper, threading.Lock()
    return a


POSE = [-236, -3362, 1074, 341, 20419, 14665]


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """프로브는 1.5초를 기다린다 — 테스트에서까지 기다릴 이유가 없다."""
    monkeypatch.setattr(Arm, "PROBE_SETTLE_S", 0.0)


# ── 판별 ────────────────────────────────────────────────────────────────────

def test_a_slave_is_called_a_slave_even_with_a_stale_command_cache():
    """⚠ 이게 실기에서 깨진 경우다. 지령 캐시가 차 있고 갱신은 멈춘 슬레이브."""
    piper = FakePiper(POSE, slave=True, ctrl_cache=POSE, ctrl_lead=-99.0)
    out = arm_with(piper).probe_command_response()
    assert out["ok"], out
    assert out["is_master"] is False, f"슬레이브를 마스터라 했다: {out}"
    assert out["moved_raw"] > out["commanded_raw"] // 2, out


def test_a_master_is_still_called_a_master():
    """마스터는 명령을 무시하고 피드백이 얼어붙는다 — 그래서 안 움직인 것으로 읽힌다."""
    piper = FakePiper(POSE, slave=False, ctrl_cache=POSE, ctrl_lead=-99.0)
    out = arm_with(piper).probe_command_response()
    assert out["ok"] and out["is_master"] is True, out
    assert out["moved_raw"] == 0, out


def test_the_probe_never_reads_its_own_command_back():
    """⚠ 지령 레지스터를 읽으면 **팔이 무엇이든 같은 답**이 나온다 — 우리가 방금
    쓴 값을 되읽는 것이라 판별이 아니라 루프백 측정이 된다."""
    src = code_only(inspect.getsource(Arm.probe_command_response))
    assert "read_joints_feedback" in src, "판별이 피드백 전용 읽기를 안 쓴다"
    assert "read_joints_raw" not in src, "판별이 지령 섞인 읽기를 다시 쓴다"

    fb = inspect.getsource(Arm.read_joints_feedback)
    assert "GetArmJointCtrl" not in fb, "피드백 전용이어야 할 곳에 지령이 섞였다"


# ── raw 읽기의 신선도 ───────────────────────────────────────────────────────

def test_a_stale_command_cache_no_longer_masks_live_feedback():
    """⚠ 실측: can0 의 raw 읽기가 joint2·joint3 을 0 으로 내면서 나머지는
    실제값을 냈다 — 지령 분기가 통째로 반환된 모습이다."""
    stale = [-236, 0, 0, 347, 20420, 14665]
    piper = FakePiper(POSE, slave=True, ctrl_cache=stale, ctrl_lead=-99.0)
    assert arm_with(piper).read_joints_raw() == POSE, "얼어붙은 지령이 피드백을 가린다"


def test_a_fresher_command_still_wins():
    """⚠ 폴백을 없애면 안 된다. 마스터 팔은 피드백을 아예 안 보내서 지령이
    그 팔의 실제 위치다 — 그래서 '더 신선하면' 지령을 쓴다."""
    live_ctrl = [1, 2, 3, 4, 5, 6]
    piper = FakePiper(POSE, slave=False, ctrl_cache=live_ctrl, ctrl_lead=1.0)
    assert arm_with(piper).read_joints_raw() == live_ctrl


def test_both_readers_use_the_same_freshness_rule():
    """같은 사실을 두 함수가 다르게 답하면 어느 쪽이 맞는지 알 길이 없다."""
    for fn in (Arm.read_joints_raw, Arm.read_joints_normalized):
        assert "_CTRL_FRESHER_S" in inspect.getsource(fn), f"{fn.__name__} 이 나이를 안 본다"
