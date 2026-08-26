"""텔레오퍼레이션 1단계 — **멈추는 길부터** (feature/teleoperation.md §6).

움직이는 코드는 아직 없다. 그런데 배타 표와 E-stop 대상에는 먼저 올라야 한다:
기능을 넣고 나서 멈추는 길을 만들면 **멈출 수 없는 기능이 존재하는 구간**이 생긴다.
"""

import inspect

import pytest

from app.services import exclusivity as ex
from app.services.teleop import TeleopSession


def test_teleop_is_an_estop_target():
    """팔을 물리적으로 움직이는 것은 전부 대상이라는 refactor #10 규칙 그대로다."""
    assert ex.Activity.TELEOP in ex.ESTOP_TARGETS
    assert ex.Activity.TELEOP in ex.STOPPERS, "정지 방법이 없으면 대상이어도 소용없다"


def test_teleop_and_the_arm_movers_block_each_other():
    """같은 팔을 둘이 밀면 안 된다."""
    for other in (ex.Activity.INFERENCE, ex.Activity.RECORDING, ex.Activity.ORCHESTRATOR):
        assert other in ex.BLOCKED_BY[ex.Activity.TELEOP], f"{other} 를 안 막는다"
        assert ex.Activity.TELEOP in ex.BLOCKED_BY[other], f"{other} 가 teleop 을 안 막는다"


def test_training_does_not_block_teleop():
    """학습은 GPU 만 쓴다 — 팔과 무관하다. 막으면 쓸데없이 손이 묶인다."""
    assert ex.Activity.TRAINING not in ex.BLOCKED_BY[ex.Activity.TELEOP]


def test_the_table_does_not_lie_about_teleop():
    """막는다고 적어놓고 상태를 안 보면 그 표를 아무도 안 믿는다."""
    assert ex.Activity.TELEOP in ex.STATE_PROVIDERS

    s = TeleopSession()
    assert not s.is_running
    assert s.start("can1", "joint")[0]
    assert s.is_running
    assert not s.start("can0", "joint")[0], "두 번째 세션이 열린다"
    s.stop()
    assert not s.is_running


def test_the_gateway_does_not_cut_torque_itself():
    """⚠ 게이트웨이가 멈춰 있으면 못 한다 — 그때가 정확히 E-stop 이 필요한 순간이다.

    토크 차단은 팔을 쥔 robotd 가 알림을 **직접 듣고** 한다.
    """
    src = inspect.getsource(TeleopSession.kill)
    assert "disable_torque" not in src, "게이트웨이가 토크를 끊으려 한다"
    assert "robotd" in src, "누가 끊는지 안 적혀 있다"


def test_robotd_listens_for_the_estop_itself():
    """estopd 에게 부탁하면 안 된다 — estopd 는 "남이 응답 못 하는 상황"을 위해 있다."""
    from pathlib import Path

    robotd = (Path(__file__).resolve().parents[2] / "daemons" / "robotd.py").read_text()
    assert "estop_events" in robotd, "E-stop 알림을 안 듣는다"
    assert "disable_all_torque" in robotd, "듣기만 하고 토크를 안 끊는다"

    estopd = (Path(__file__).resolve().parents[2] / "daemons" / "estopd.py").read_text()
    assert "disable_torque" not in estopd and "rpc_call" not in estopd, \
        "estopd 가 남에게 부탁한다 — 그 남도 응답 못 할 수 있다"


def test_cutting_torque_survives_one_arm_failing():
    """부분 성공이라도 해야 한다 — estopd 가 PID 를 죽일 때와 같은 규율."""
    pytest.importorskip("piper_robot")
    from piper_robot.hub import RobotHub

    class _Arm:
        # `is_master` 로 마스터 팔을 거른다 (test_estop_torque_scope.py 참고).
        # 여기서 보는 것은 그 갈래가 아니라 **부분 실패** 이므로 둘 다 슬레이브다.
        is_master = False

        def __init__(self, ok): self._ok = ok
        def disable_torque(self):
            if not self._ok:
                raise RuntimeError("CAN 오류")
            return True

    hub = RobotHub()
    hub.arms = {"can0": _Arm(False), "can1": _Arm(True)}
    assert hub.disable_all_torque() == ["can1"], "하나가 실패하면 나머지도 포기한다"


def test_the_estop_listener_reconnects():
    """Redis 가 잠깐 끊겼다고 듣기를 그만두면 **안전 장치가 조용히 사라진다.**"""
    pytest.importorskip("piper_bus")
    from piper_bus.client import Bus

    src = inspect.getsource(Bus.estop_events)
    assert "while stop is None or not stop.is_set()" in src
    assert "except Exception" in src and "time.sleep" in src, "끊기면 포기한다"
