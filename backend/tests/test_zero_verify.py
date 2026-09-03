"""영점 굽기 — **팔의 "성공" 을 그대로 믿지 않는다.**

⚠ 2026-09-03 실기: 영점을 16번 눌러 전부 `200 OK` 를 받았는데 **하나도 안
굽혔다.** 팔이 `is_set_zero_successfully=1` 로 응답했기 때문이다. 화면에는
아무 신호도 없어서, 사람은 굽힌 줄 알고 다음 관절로 넘어갔다.

영점이 옮겨졌으면 그 관절은 0 을 보고해야 한다. 확인할 근거가 바로 옆에
있는데 안 보는 것은 성공을 지어내는 것이다.
"""

import inspect
import textwrap
import threading

import pytest
from conftest import python_code_only
from piper_robot.arm import Arm


class FakePiper:
    """`applied=False` 면 성공이라 응답하면서 값은 안 바꾼다 — 실기 그대로."""

    def __init__(self, raw: int, *, applied: bool, flag: int = 1):
        self._raw, self._applied, self._flag = raw, applied, flag
        self.sent_zero = 0
        self.mode = 0x00                      # Standby — 리셋 직후의 상태
        self.enabled = {i: False for i in range(1, 7)}

    def ClearRespSetInstruction(self): pass
    def GripperCtrl(self, *a): pass

    # ── 굽기 전 준비 (`_prepare_for_config_locked`) ──
    #
    # ⚠ 실기에서 이 준비가 **빠져 있어서** 굽기가 조용히 무시됐다. 가짜 팔도
    #   같은 상태를 흉내내야, 준비 없이 굽는 회귀를 테스트가 잡는다.
    def EnableArm(self, motor_num=7, *a):
        for i in range(1, 7):
            if motor_num in (7, i):
                self.enabled[i] = True

    def DisableArm(self, motor_num=7, *a):
        for i in range(1, 7):
            if motor_num in (7, i):
                self.enabled[i] = False

    def ModeCtrl(self, ctrl_mode=0x01, *a):
        self.mode = ctrl_mode

    def GetArmStatus(self):
        mode = self.mode
        return type("S", (), {"arm_status": type("A", (), {"ctrl_mode": mode})()})()

    def GetArmLowSpdInfoMsgs(self):
        en = self.enabled
        return type("L", (), {
            f"motor_{i}": type("M", (), {
                "foc_status": type("F", (), {"driver_enable_status": en[i]})()})()
            for i in range(1, 7)})()

    def JointConfig(self, joint_num=7, set_zero=0, **kw):
        self.sent_zero += 1
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
    a.is_master = False          # 기본은 슬레이브 — 마스터는 테스트가 따로 세운다
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

def test_a_master_arm_is_refused_before_anything_is_sent():
    """⚠ **마스터(示教输入臂)는 외부 제어 명령을 전부 무시한다.** 실측(can3):
    `EnablePiper` · `ModeCtrl` · `DisableArm` 을 보내도 `ctrl_mode` 는 Standby
    그대로고 토크도 하나도 안 켜졌다. 굽기도 같다 — 그런데 팔은 **성공이라
    응답하므로** 보내고 나서는 구분이 안 된다. 보내기 전에 막아야 이유를 말할 수 있다.
    """
    piper = FakePiper(1234, applied=False)
    arm = arm_with(piper)
    arm.is_master = True

    out = arm.set_hardware_zero("joint1")
    assert out["ok"] is False
    assert "마스터" in out["error"] and "무시" in out["error"], out
    assert piper.sent_zero == 0, "막았다면서 프레임을 보냈다"


def test_a_slave_arm_is_not_blocked_by_that_guard():
    arm = arm_with(FakePiper(4880, applied=True))
    arm.is_master = False
    assert arm.set_hardware_zero("joint1")["ok"] is True


def test_the_flash_prepares_the_arm_itself():
    """⚠ **굽기가 스스로 상태를 세워야 한다.** 토크 버튼이 세워 둔 상태를 사이에
    끼어든 0x150 리셋이 지운다 — 실측(2026-09-03): 리셋 후 `CAN ctrl → Standby`,
    모터 전부 꺼짐. 그 상태로 나간 굽기는 조용히 무시됐고, 준비를 다시 세우자
    같은 팔이 `18714 → 0` 으로 먹었다.

    순서에 기대지 않는다는 것이 요점이다 — 앞에 무엇이 지나갔든 여기서 세운다."""
    piper = FakePiper(5000, applied=True)
    assert piper.mode == 0x00 and not any(piper.enabled.values())

    out = arm_with(piper).set_hardware_zero("joint3")
    assert out["ok"] is True, out
    assert piper.mode == 0x01, "CAN 제어 모드로 안 세운다"
    # 대상만 꺼지고 나머지는 켜져 있어야 한다 (공식 예제가 만드는 상태)
    assert piper.enabled[3] is False, "대상 모터를 실능시키지 않는다"
    assert all(piper.enabled[i] for i in (1, 2, 4, 5, 6)), piper.enabled


def test_waiting_is_on_state_not_on_a_fixed_delay():
    """⚠ 고정 딜레이로는 부족했다. 리셋이 팔을 Standby 로 떨어뜨린 뒤 100ms 로는
    CAN 제어 모드로 못 돌아오고, 그 상태로 나간 굽기가 무시됐다. 공식 예제도
    `while not EnablePiper(): sleep(0.01)` 로 **기다린다.**"""
    import inspect

    src = python_code_only(textwrap.dedent(
        inspect.getsource(Arm._prepare_for_config_locked)))
    assert "PREPARE_TIMEOUT_S" in src, "기다리지 않는다"
    assert "_mode_int_locked" in src and "_enabled_locked" in src, "상태를 안 본다"
    assert "EnablePiper" not in src, "한 번 부르면 안 먹는 EnablePiper 를 쓴다"


# ── 재시도 ──────────────────────────────────────────────────────────────────

class FlakyPiper(FakePiper):
    """`lands_at` 번째 시도에서야 먹는 팔 — 설정 프레임이 떨어지는 경우."""

    def __init__(self, raw: int, *, lands_at: int):
        super().__init__(raw, applied=False)
        self._lands_at = lands_at

    def JointConfig(self, joint_num=7, set_zero=0, **kw):
        self.sent_zero += 1
        if self.sent_zero >= self._lands_at:
            self._raw = 0


def test_a_dropped_config_frame_is_retried():
    """⚠ **설정 프레임이 간헐적으로 떨어진다.** 실측(can3): 1회째 `1994 → 1993`
    으로 안 먹고 2회째 `1993 → 0` 으로 먹었다. can0 도 2회째에 `29890 → 0`.

    재시도가 없어서 팔 두 대가 "영점이 안 된다" 로 보였다 — 코드는 멀쩡했고
    한 번 더 보내면 되는 일이었다."""
    piper = FlakyPiper(29890, lands_at=2)
    out = arm_with(piper).set_hardware_zero("joint5")
    assert out["ok"] is True, out
    assert out["raw_after"] == 0
    assert out["attempts"] == 2, out


def test_retrying_is_safe_because_the_flash_is_idempotent():
    """⚠ 되돌릴 수 없는 조작을 반복해도 되는 이유는 **멱등**이기 때문이다 —
    이미 0 인 관절에 다시 구우면 `0 → 0` 이다(실측 3·4·5회). 이 성질이 없으면
    재시도가 위험한 짓이 된다."""
    piper = FakePiper(0, applied=True)
    for _ in range(3):
        out = arm_with(piper).set_hardware_zero("joint1")
        assert out["ok"] is True and out["raw_after"] == 0, out


def test_it_gives_up_after_a_bounded_number_of_tries():
    """영원히 두드리면 사람이 화면 앞에서 하염없이 기다린다."""
    piper = FakePiper(5000, applied=False)
    out = arm_with(piper).set_hardware_zero("joint1")
    assert out["ok"] is False
    assert piper.sent_zero == Arm.ZERO_ATTEMPTS, piper.sent_zero


def test_an_explicit_refusal_is_not_retried():
    """⚠ 팔이 "실패" 라고 응답한 것과 프레임이 떨어진 것은 다르다. 전자는 다시
    보내도 같은 답이 오므로, 두드리는 대신 사람에게 말한다."""
    piper = FakePiper(5000, applied=False, flag=0)
    out = arm_with(piper).set_hardware_zero("joint1")
    assert out["ok"] is False and piper.sent_zero == 1, piper.sent_zero

    master = arm_with(FakePiper(5000, applied=False))
    master.is_master = True
    assert master.set_hardware_zero("joint1")["ok"] is False

def test_nothing_gates_the_flash_on_torque_state():
    """⚠ 한때 "모터가 활성이면 거절" 하는 가드가 있었다. 굽기가 스스로 준비하게
    된 뒤로 그 가드는 **멀쩡한 시도를 전부 거절한다** — 평상시엔 토크가 켜져
    있기 때문이다. 진짜 원인은 모터 상태가 아니라 검증·재시도가 없던 것이었다.

    그래서 화면에도 토크 버튼이 없다. 사람이 순서를 맞춰 줘야 하는 절차처럼
    보였던 것이 실은 우리가 결과를 안 본 탓이었다."""
    import inspect
    from pathlib import Path

    from app.routers import robots

    src = python_code_only(textwrap.dedent(inspect.getsource(robots.set_hardware_zero)))
    assert "motor_enabled" not in src, "토크 상태로 굽기를 막는다"

    modal = (Path(__file__).resolve().parents[2] / "frontend" / "src"
             / "components" / "ZeroCalibrationModal.tsx").read_text()
    assert "토크 끊기" not in modal and "토크 걸기" not in modal, "창에 토크 버튼이 남았다"
    assert "/robots/motor/torque" not in modal, "창이 아직 토크를 만진다"


def test_the_flash_still_does_the_disabling_itself():
    """버튼을 없앨 수 있는 근거 — 굽기가 대상 모터를 스스로 실능시킨다.
    이게 빠지면 버튼도 없고 절차도 없는 상태가 된다."""
    piper = FakePiper(5000, applied=True)
    piper.EnableArm(7)                      # 평상시처럼 토크가 켜져 있다
    assert all(piper.enabled.values())

    out = arm_with(piper).set_hardware_zero("joint2")
    assert out["ok"] is True, out
    assert piper.enabled[2] is False, "대상 모터를 스스로 끄지 않는다"
