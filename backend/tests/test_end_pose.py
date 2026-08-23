"""말단 조그 (feature/teleoperation.md §3-C, §4).

⚠ **관절 안전 필터가 안 걸리는 유일한 모드다.** 관절을 팔의 온보드 IK 가 정하므로
`filter_goal` 의 범위 클램프도 변화율 램프도 지나가지 않는다. 그래서 여기서
잠그는 것은 *그 대신 막는 것들*이다 — 상자, 걸음 상한, 도달 확인.
"""

import inspect

import pytest

pytest.importorskip("piper_robot")
from piper_robot import endpose as E  # noqa: E402

HOME = {"x": 300_000, "y": 0, "z": 200_000, "rx": 0, "ry": 0, "rz": 0}


def test_a_step_moves_by_what_was_asked():
    t, why = E.step_target(HOME, "x", 10, E.WorkspaceBox())
    assert t["x"] == 310_000, why          # 10mm = 10000 (0.001mm 단위)
    assert t["y"] == HOME["y"], "안 시킨 축이 움직였다"


def test_rotation_uses_degrees_not_millimetres():
    """단위가 둘이라 섞이면 **1000배 틀린다** — 깊이 단위에서 이미 겪었다."""
    t, _ = E.step_target(HOME, "rx", 5, E.WorkspaceBox())
    assert t["rx"] == 5_000


def test_a_target_outside_the_box_is_refused_not_clamped():
    """⚠ 클램프해 보내면 **시킨 것과 다른 곳으로 간다** — 사용자는 이유를 모른다.

    거절하고 말하는 편이, 조용히 모서리로 미끄러지는 것보다 낫다.
    """
    t, why = E.step_target(dict(HOME, x=495_000), "x", 20, E.WorkspaceBox())
    assert t is None
    assert "작업 공간" in why and "X" in why


def test_the_step_size_is_capped():
    """버튼 한 번에 팔이 반대편으로 가면 안 된다."""
    assert E.clamp_step("x", 999) == E.MAX_STEP_MM
    assert E.clamp_step("rx", -999) == -E.MAX_STEP_DEG


def test_an_unknown_axis_is_rejected():
    with pytest.raises(ValueError):
        E.clamp_step("w", 1)


def test_the_box_starts_narrow():
    """⚠ 팔 컨트롤러가 자체 한계를 갖는다고 **가정하지 않는다.**

    넓게 시작하면 처음 눌러보는 순간이 실험이 된다. 실측 후 넓히는 방향으로.
    """
    box = E.WorkspaceBox()
    for lo, hi in (box.x, box.y, box.z):
        assert hi - lo <= 700, f"상자가 넓다: {lo}~{hi}"


def test_not_reaching_the_target_is_detected():
    """IK 해가 없는 곳을 계속 밀면 팔이 떨거나 특이점에서 튄다."""
    target = dict(HOME, x=400_000)
    assert E.reached(target, target)
    assert not E.reached(target, HOME), "100mm 를 못 갔는데 도달로 본다"


def test_reaching_allows_a_little_slack():
    """정확히 같은 값을 요구하면 늘 실패한다 — 서보에는 오차가 있다."""
    target = dict(HOME, x=400_000)
    assert E.reached(target, dict(target, x=402_000))


# ── 배선 ────────────────────────────────────────────────────────────────────

def test_the_arm_uses_point_to_point_end_mode():
    """관절 모드(MOVE J)로 보내면 `EndPoseCtrl` 이 안 먹는다."""
    from piper_robot.arm import Arm

    src = inspect.getsource(Arm.move_end_pose)
    assert "ModeCtrl(0x01, 0x00" in src, "MOVE P 로 안 바꾼다"
    assert "EndPoseCtrl" in src


def test_the_end_pose_speed_is_fixed_low():
    """관절 필터가 안 걸리는 모드라 속도가 사람 반응 범위를 넘으면 안 된다."""
    from piper_robot.arm import Arm

    assert Arm.END_POSE_SPEED <= 30


def test_the_hub_checks_the_box_before_commanding():
    """상자를 통과한 뒤에야 명령이 나가야 한다 — 순서가 뒤집히면 막는 의미가 없다."""
    from piper_robot.hub import RobotHub

    src = inspect.getsource(RobotHub.jog_end_pose)
    assert src.index("step_target") < src.index("move_end_pose")
    assert "reached(" in src, "도달 확인을 안 한다"


def test_the_route_never_takes_an_absolute_pose():
    """절대 좌표를 받으면 **오타 하나가 큰 이동**이 된다."""
    from app.routers import robots

    src = inspect.getsource(robots.EndPoseJogRequest)
    assert "delta" in src
    for absolute in ("x:", "target", "pose:"):
        assert absolute not in src, f"절대 좌표를 받는다: {absolute}"


def test_the_blocking_move_does_not_run_on_the_event_loop():
    """도달을 2초 기다린다 — 이벤트 루프에서 돌리면 그동안 heartbeat 도 멈춘다."""
    from app.routers import robots

    src = inspect.getsource(robots.end_pose_jog)
    assert "run_in_executor" in src


# ── 데몬 RPC 등록 (이번 세션에서 네 번 걸린 자리) ──────────────────────────

@pytest.mark.parametrize("daemon,hub_cls", [("robotd", "RobotHub"), ("rsd", "RealSenseHub")])
def test_every_method_the_gateway_calls_is_registered(daemon, hub_cls):
    """⚠ 데몬은 `_METHODS` 에 없는 메서드를 **조용히 거부한다.**

    빠뜨린 것과 고장난 RPC 가 똑같이 보인다 — 이번 세션에서만 네 번 걸렸다
    (`set_background_mask`, `calibrate_gray_card`, `measure_gray_card`,
    `start_identify`). 게이트웨이가 부르는 이름이 목록에 다 있는지 대조한다.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    src = (root / "daemons" / f"{daemon}.py").read_text()
    listed = set(re.findall(r'"([a-z_]+)"', src.split("_METHODS = {", 1)[1].split("}", 1)[0]))

    client = {"robotd": "robot_manager", "rsd": "realsense_manager"}[daemon]
    calls = set(re.findall(r'_call\(\s*"([a-z_]+)"',
                           (root / "backend" / "app" / "services" / f"{client}.py").read_text()))
    calls |= set(re.findall(r'self\._call\(\s*"([a-z_]+)"',
                            (root / "backend" / "app" / "services" / f"{client}.py").read_text()))
    missing = sorted(calls - listed)
    assert not missing, f"{daemon} 의 _METHODS 에 없다 — 조용히 거부된다: {missing}"


def test_the_screen_offers_steps_not_coordinates():
    """절대 좌표 입력란이 생기면 오타 하나가 큰 이동이 된다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components"
           / "EndPosePanel.tsx").read_text()
    assert "end-pose/jog" in src and "delta" in src
    assert "axis, -(step" in src and "axis, step" in src, "±버튼이 아니다"


def test_the_screen_shows_the_workspace_box():
    """어디까지 갈 수 있는지 모르면, 거절당하고 나서야 한계를 배운다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components"
           / "EndPosePanel.tsx").read_text()
    assert "작업 공간" in src and "box.x" in src


def test_the_refusal_message_comes_from_the_backend():
    """상자 밖인지 도달 실패인지 화면이 판정하면 두 곳에서 갈린다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components"
           / "EndPosePanel.tsx").read_text()
    assert "e instanceof Error ? e.message" in src
    assert "작업 공간 밖" not in src.split("const jog", 1)[1].split("}", 1)[0]
