"""robotd 안전층 — 하드 리밋·데드맨 (refactor/robotd-safety.md).

**전부 하드웨어 없이 돈다.** 그게 필터를 순수 함수로 만든 이유다 —
안전 로직을 실제 팔로 검증하는 건 그 자체가 위험하다.
"""

import pytest

pytest.importorskip("piper_robot")
from piper_robot import JOINT_ORDER, Reason, SafetyConfig, filter_goal  # noqa: E402
from piper_robot.safety import clamp_range  # noqa: E402

HOME = dict.fromkeys(JOINT_ORDER, 0.0)


def _pose(**over):
    return {**HOME, **over}


def test_normal_command_passes_through_untouched():
    """평범한 명령을 건드리면 안 된다. 안전층이 늘 개입하면 그건 고장이다."""
    goal = _pose(joint1=5.0, joint3=-7.5, gripper=40.0)
    applied, why = filter_goal(HOME, goal, SafetyConfig())
    assert why is Reason.OK
    assert applied == goal


def test_out_of_range_is_clamped_not_rejected():
    """범위 밖은 **자른다**, 거부하지 않는다.

    거부하면 정책이 계속 범위 밖을 명령하는 동안 팔이 굳는다.
    자르면 도달 가능한 가장 가까운 자세로 가고 불연속이 없다.
    """
    cfg = SafetyConfig(max_step=1000.0)          # 변화량 제한은 비켜둔다
    applied, why = filter_goal(HOME, _pose(joint1=250.0, joint2=-250.0), cfg)
    assert why is Reason.CLAMPED_RANGE
    assert applied["joint1"] == 100.0
    assert applied["joint2"] == -100.0


def test_gripper_has_a_different_range():
    """그리퍼만 0..100 이다. -100 을 허용하면 **닫힘 방향으로 두 배** 명령된다."""
    assert clamp_range("gripper", -5.0) == (0.0, True)
    assert clamp_range("gripper", 150.0) == (100.0, True)
    assert clamp_range("joint1", -5.0) == (-5.0, False)


def test_sitting_at_a_joint_limit_is_not_a_violation():
    """**회귀** — 팔이 한계에 앉아 있으면 매 프레임 경고가 떴다.

    파킹 자세가 joint2 = -100, joint3 = +100 이라 흔한 상태인데, 그 값이 shm 을
    float32 로 왕복하면 100.0000076 으로 돌아온다. 자르기는 맞지만 **보고하면**
    진짜 위반이 그 소음에 묻힌다.
    """
    import struct

    def f32(v):
        return struct.unpack("<f", struct.pack("<f", v))[0]

    for v in (100.0, -100.0):
        got, reported = clamp_range("joint2", f32(v))
        assert got == pytest.approx(v)
        assert not reported, f"한계에 앉은 것을 위반으로 본다: {f32(v)!r}"

    # 진짜 위반은 자릿수가 다르다 — 그건 보고해야 한다
    assert clamp_range("joint2", 100.5)[1]

    _, why = filter_goal(_pose(joint2=f32(-100.0)), _pose(joint2=f32(-100.0)),
                         SafetyConfig())
    assert why is Reason.OK


def test_step_limit_caps_a_jump():
    """한 스텝에 튀는 명령을 잘라 **최대 변화량만큼만** 움직인다.

    정책이 어긋난 관측을 받으면 한 프레임에 전 구간을 건너뛰는 목표가 나온다.
    그걸 그대로 보내면 팔이 최고속으로 날아간다.
    """
    cfg = SafetyConfig(max_step=10.0)
    applied, why = filter_goal(_pose(joint4=30.0), _pose(joint4=90.0), cfg)
    assert why is Reason.CLAMPED_RATE
    assert applied["joint4"] == pytest.approx(40.0)

    # 반대 방향도 같은 크기로
    applied, _ = filter_goal(_pose(joint4=30.0), _pose(joint4=-90.0), cfg)
    assert applied["joint4"] == pytest.approx(20.0)


def test_step_limit_can_differ_per_joint():
    cfg = SafetyConfig(max_step=5.0, max_step_per_joint={"gripper": 100.0})
    applied, _ = filter_goal(HOME, _pose(joint1=50.0, gripper=80.0), cfg)
    assert applied["joint1"] == pytest.approx(5.0)
    assert applied["gripper"] == pytest.approx(80.0), "그리퍼는 빨라도 된다"


def test_rate_limit_wins_over_range_in_the_reason():
    """둘 다 걸리면 **더 강한 쪽**을 보고한다. 로그에서 원인을 하나만 읽고 싶다."""
    applied, why = filter_goal(HOME, _pose(joint1=999.0), SafetyConfig(max_step=10.0))
    assert why is Reason.CLAMPED_RATE
    assert applied["joint1"] == pytest.approx(10.0)


def test_nan_and_inf_never_reach_can():
    """NaN/inf 는 **비교를 전부 통과한다** — 클램프만으로는 못 막는다.

    `nan > 100` 도 `nan < -100` 도 False 라 그대로 빠져나가고,
    `int(nan)` 은 SDK 안에서 터지거나 쓰레기 값이 된다.
    """
    for bad in (float("nan"), float("inf"), float("-inf")):
        applied, why = filter_goal(_pose(joint2=12.0), _pose(joint2=bad), SafetyConfig())
        assert why is Reason.NOT_FINITE
        assert applied["joint2"] == pytest.approx(12.0), "현재 자세를 유지해야 한다"
        assert all(v == v for v in applied.values())


def test_missing_joints_hold_position():
    """일부만 온 명령을 0으로 채우지 않는다.

    0은 정규화 좌표의 "가운데"라 그럴듯해 보이고, 그게 명령이 되면 팔이 튄다.
    """
    now = _pose(joint1=30.0, joint5=-20.0, gripper=70.0)
    applied, why = filter_goal(now, {"joint1": 32.0}, SafetyConfig())
    assert why is Reason.OK
    assert applied["joint1"] == pytest.approx(32.0)
    assert applied["joint5"] == pytest.approx(-20.0), "안 보낸 관절이 0으로 갔다"
    assert applied["gripper"] == pytest.approx(70.0)
    assert set(applied) == set(JOINT_ORDER), "관절이 빠지면 SDK 호출이 KeyError 로 죽는다"


def test_deadman_holds_position_rather_than_cutting_torque():
    """정지 = **그 자리에 서기**. 토크를 끊으면 팔이 중력으로 떨어진다."""
    now = _pose(joint2=45.0, joint3=-60.0)
    applied, why = filter_goal(now, _pose(joint2=90.0), SafetyConfig(),
                               deadman_tripped=True)
    assert why is Reason.DEADMAN
    assert applied == now, "데드맨인데 목표가 반영됐다"


def test_deadman_outranks_every_other_reason():
    """소비자가 죽었으면 그 명령은 **아예 보지 않는다.**"""
    _, why = filter_goal(HOME, _pose(joint1=float("nan")), SafetyConfig(),
                         deadman_tripped=True)
    assert why is Reason.DEADMAN


def test_filter_is_pure():
    """부작용이 없어야 기존 데이터셋에 리플레이할 수 있다.

    "켰다면 몇 %의 프레임에서 발동했을까"를 **로봇을 켜기 전에** 재는 게 목적이다.
    """
    now, goal = _pose(joint1=10.0), _pose(joint1=200.0)
    now_copy, goal_copy = dict(now), dict(goal)
    cfg = SafetyConfig()

    a1, r1 = filter_goal(now, goal, cfg)
    a2, r2 = filter_goal(now, goal, cfg)

    assert now == now_copy and goal == goal_copy, "입력을 변형했다"
    assert (a1, r1) == (a2, r2), "같은 입력에 다른 출력 — 순수하지 않다"
    a1["joint1"] = 999.0
    assert filter_goal(now, goal, cfg)[0]["joint1"] != 999.0, "반환값이 내부 상태다"


def test_step_limit_zero_disables_rate_capping():
    """0은 "제한 없음"이다 — 0으로 두면 팔이 굳는 게 아니라 자유로워야 한다."""
    applied, why = filter_goal(HOME, _pose(joint1=90.0), SafetyConfig(max_step=0.0))
    assert why is Reason.OK
    assert applied["joint1"] == pytest.approx(90.0)


def test_repeated_filtering_converges_to_the_goal():
    """제한이 걸려도 **결국 목표에 도달한다.** 매 스텝 조금씩 가까워져야 한다.

    여기가 틀리면(예: 항상 현재 자세 기준이 아니라 원점 기준으로 자르면)
    팔이 목표 근처에서 영원히 못 간다.
    """
    cfg = SafetyConfig(max_step=10.0)
    now = dict(HOME)
    goal = _pose(joint1=95.0)
    for _ in range(20):
        now, _ = filter_goal(now, goal, cfg)
    assert now["joint1"] == pytest.approx(95.0)


# ── 브리지에 붙은 안전층 (실제 CAN 없이) ──
#
# 브리지는 robotd 안에 있다(`piper_robot.publish`). 게이트웨이에도 사본이 있었는데,
# 같은 로직이 두 벌이면 어느 쪽이 도는지 알 수 없어 지웠다.

def _fake_arm(iface="pytest-can-safety", pose=None):
    """`ArmInfo` 자리에 세우는 가짜. CAN 대신 호출을 기록한다."""
    pose = pose or dict.fromkeys(JOINT_ORDER, 0.0)

    class _Piper:
        def __init__(self):
            self.joint_cmds = []

        def ModeCtrl(self, *a):
            pass

        def JointCtrl(self, *a):
            self.joint_cmds.append(a)

        def GripperCtrl(self, *a):
            pass

    class _Arm:
        def __init__(self):
            import threading

            self.iface = iface
            self.connected = True
            self._piper = _Piper()
            self._lock = threading.Lock()
            self._pose = dict(pose)

        def read_joints_normalized(self):
            return dict(self._pose)

        def read_error(self):
            return {"err_code": 0, "flags": []}

        def refresh_ctrl_mode(self):
            return 1

    return _Arm()


def test_every_can_command_passes_the_filter():
    """**CAN 으로 나가는 경로는 하나여야 한다.**

    필터를 우회하는 송신 경로가 있으면 그게 구멍이다. `_send` 만 SDK 를 만지는지
    확인한다 — `_hold` 도 `_send` 를 거쳐야 한다.
    """
    import ast
    import inspect

    from piper_robot import publish as arm_bridge

    src = inspect.getsource(arm_bridge.ArmBridge)
    tree = ast.parse(src.lstrip())
    touching = {
        fn.name
        for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
        for n in ast.walk(fn) if isinstance(n, ast.Call)
        and ast.unparse(n.func).endswith(("JointCtrl", "GripperCtrl", "ModeCtrl"))
    }
    assert touching == {"_send"}, f"필터를 우회해 CAN 을 만지는 곳이 있다: {touching}"

    send = inspect.getsource(arm_bridge.ArmBridge._send)
    calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(send.lstrip()))
             if isinstance(n, ast.Call)}
    assert "filter_goal" in calls, "_send 가 안전층을 안 거친다"


def test_bridge_clamps_a_wild_goal_before_can():
    from piper_robot.publish import ArmBridge
    from piper_robot import SafetyConfig

    arm = _fake_arm()
    bridge = ArmBridge(arm, safety=SafetyConfig(max_step=10.0))
    bridge._send({**HOME, "joint1": 500.0})

    assert bridge.filtered == 1
    assert bridge.last_reason is Reason.CLAMPED_RATE
    (sent,) = arm._piper.joint_cmds
    # raw 로 변환된 뒤라도 **범위 안**이어야 한다
    from piper_robot import JOINT_CALIBRATION

    lo, hi = JOINT_CALIBRATION["joint1"]
    assert lo <= sent[0] <= hi, f"범위 밖 raw 가 CAN 으로 나갔다: {sent[0]}"


def test_deadman_actively_commands_the_current_pose():
    """**명령을 멈추는 것과 팔을 세우는 것은 다르다.**

    마지막 명령이 먼 목표였으면 팔은 소비자가 죽은 뒤에도 계속 그리로 간다.
    데드맨은 현재 자세를 실제로 **명령해서** 세워야 한다.
    """
    from piper_robot.publish import ArmBridge
    from piper_robot import JOINT_CALIBRATION, denormalize_joint

    here = {**HOME, "joint2": 33.0}
    arm = _fake_arm(pose=here)
    bridge = ArmBridge(arm)
    bridge._hold(first=True, deadman_ms=300)

    assert bridge._deadman_held
    assert bridge.last_reason is Reason.DEADMAN
    assert arm._piper.joint_cmds, "데드맨인데 CAN 으로 아무것도 안 나갔다"
    assert arm._piper.joint_cmds[-1][1] == denormalize_joint("joint2", 33.0)
    assert JOINT_CALIBRATION  # 계약이 살아있는지 (import 가 죽으면 위 단언이 무의미)


def test_a_narrow_calibration_is_reported_as_such():
    """**증상이 같은 두 원인을 구분한다.**

    실기에서 겪었다: joint3 의 캘리브레이션 최대가 0 인데 팔이 raw 2103 에 앉아
    있어서, 현재 자세를 그대로 되보내는 정상 명령마다 범위 위반이 떴다.
    "명령이 이상하다"와 "캘리브레이션이 좁다"를 한 사유로 뭉뚱그리면
    정책을 의심해야 할지 캘리브레이션을 의심해야 할지 로그만으로는 알 수 없다.
    """
    # 현재 자세가 이미 범위 밖 — 캘리브레이션 문제
    out = _pose(joint3=102.47)
    _, why = filter_goal(out, dict(out), SafetyConfig(max_step=0.0))
    assert why is Reason.STATE_OUT_OF_RANGE

    # 자세는 멀쩡한데 명령이 범위 밖 — 명령자 문제
    #
    # ⚠ joint1 을 쓴다. joint3 을 범위 밖으로 밀면 팔이 **바닥으로 내려가서**
    #   바닥 필터가 먼저 걸린다(그게 더 강한 사유다). 여기서 가리려는 것은
    #   범위와 상태의 구분이지 바닥이 아니므로, 높이를 안 바꾸는 관절을 쓴다 —
    #   joint1 은 base 의 z 축 회전이라 어떤 값에서도 최저점이 그대로다.
    _, why = filter_goal(HOME, _pose(joint1=180.0), SafetyConfig(max_step=0.0))
    assert why is Reason.CLAMPED_RANGE
