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
    assert E.reached(HOME, target, target)
    assert not E.reached(HOME, target, HOME), "100mm 를 못 갔는데 도달로 본다"


def test_reaching_allows_a_little_slack():
    """정확히 같은 값을 요구하면 늘 실패한다 — 서보에는 오차가 있다."""
    target = dict(HOME, x=400_000)
    assert E.reached(HOME, target, dict(target, x=398_000))


def test_a_step_that_did_not_move_is_never_called_reached():
    """**회귀 — 실기에서 걸렸다.**

    처음엔 절대 오차 5mm 를 뒀는데 한 걸음도 5mm 였다. Z +5mm 를 보내고 팔이
    **전혀 안 움직였는데** 오차 안이라 "도달"로 보고했다.

    시킨 거리에 견줘 본다 — 절대값으로 재면 걸음이 작을수록 검사가 무의미해진다.
    """
    target = dict(HOME, z=HOME["z"] + 5_000)     # 5mm 걸음
    assert not E.reached(HOME, target, HOME), "안 움직였는데 도달로 본다"
    # 1/5 만 간 것도 실패다
    assert not E.reached(HOME, target, dict(HOME, z=HOME["z"] + 1_000))
    # 절반 넘게 갔으면 인정
    assert E.reached(HOME, target, dict(HOME, z=HOME["z"] + 3_000))


def test_moving_the_wrong_way_is_not_reaching():
    target = dict(HOME, z=HOME["z"] + 5_000)
    assert not E.reached(HOME, target, dict(HOME, z=HOME["z"] - 5_000))


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
    # 도달 확인은 **다음 명령 때** 한다 (기다리지 않으려고) — 그쪽에 있어야 한다
    assert "reached(" in inspect.getsource(RobotHub._check_previous), "도달 확인을 안 한다"
    assert src.index("_check_previous") < src.index("move_end_pose"), \
        "직전 실패를 보기 전에 또 보낸다"


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


def test_the_pad_shows_direction_by_position():
    """⚠ 예전에는 축 라벨 옆에 작은 −/+ 두 개였다. 어느 쪽이 앞인지 매번 라벨을
    읽어야 했고 버튼이 손가락보다 작았다 — "답답하다"로 보고됐다.
    십자 배치는 누르기 전에 방향이 보인다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components"
           / "EndPosePanel.tsx").read_text()
    assert "function Pad(" in src
    assert "grid-cols-3 grid-rows-3" in src, "십자 배치가 아니다"
    assert "h-11 w-11" in src, "버튼이 손가락만큼 크지 않다"


def test_the_screen_offers_steps_not_coordinates():
    """절대 좌표 입력란이 생기면 오타 하나가 큰 이동이 된다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components"
           / "EndPosePanel.tsx").read_text()
    assert "end-pose/jog" in src and "delta" in src
    # 절대 좌표를 안 받는지가 요점이다 — 모든 호출이 **부호 있는 걸음**이어야 한다.
    # (버튼 모양은 십자 패드로 바뀌었다. 위 `test_the_pad_shows_direction_by_position`.)
    import re
    calls = re.findall(r"jog\('(\w+)', (-?)step(?:Mm|Deg)\)", src)
    assert calls, "걸음 단위 호출이 없다"


def test_every_axis_can_go_both_ways():
    """⚠ **축별로** 봐야 한다. 전체 부호 집합만 세면 다섯 축이 양방향이라
    한 축이 한 방향뿐이어도 통과한다 — 실제로 RX 를 십자 **가운데**에 넣어
    그렇게 됐다. 가운데는 자리가 하나인데 축은 양방향이 필요하다.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components"
           / "EndPosePanel.tsx").read_text()
    by: dict[str, set[str]] = {}
    for axis, sign in re.findall(r"jog\('(\w+)', (-?)step(?:Mm|Deg)\)", src):
        by.setdefault(axis, set()).add(sign or "+")
    assert set(by) == {"x", "y", "z", "rx", "ry", "rz"}, f"빠진 축: {by.keys()}"
    for axis, signs in sorted(by.items()):
        assert signs == {"+", "-"}, f"{axis} 는 {sorted(signs)} 방향뿐이다"


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


def test_an_arm_outside_the_box_can_come_back():
    """**회귀 — 실기에서 걸렸다.**

    파킹 자세가 X 55mm 였는데 상자는 100~500 이라, 목표가 밖이면 무조건 거절하는
    규칙이 **상자로 돌아가는 명령까지 막았다.** 어느 방향으로도 못 움직였다.

    클램프가 아니라 **방향 판정**으로 푼다: 시킨 곳으로 가되 나빠지는 쪽만 막는다.
    """
    box = E.WorkspaceBox()
    out = dict(HOME, x=55_000)          # 상자 밖 (X 55mm)

    back, why = E.step_target(out, "x", 5, box)
    assert back is not None, f"돌아오는 방향을 막는다: {why}"
    assert back["x"] == 60_000

    worse, why2 = E.step_target(out, "x", -5, box)
    assert worse is None, "더 나가는 방향을 허용한다"

    # 위반과 무관한 축은 움직일 수 있어야 한다 — 아니면 자세를 못 바꾼다
    sideways, _ = E.step_target(out, "z", 5, box)
    assert sideways is not None


def test_being_inside_still_means_you_cannot_leave():
    """돌아올 길을 열어준 것이 **나갈 길까지 연 것은 아니다.**"""
    box = E.WorkspaceBox()
    t, why = E.step_target(dict(HOME, x=495_000), "x", 20, box)
    assert t is None and "작업 공간" in why


def test_excursion_measures_how_far_outside():
    box = E.WorkspaceBox()
    assert box.excursion(300, 0, 200) == 0
    assert box.excursion(55, 0, 200) == pytest.approx(45)
    # 여러 축이 함께 나가면 합친다 — 한 축만 고쳐도 나아진 것으로 본다
    assert box.excursion(55, 400, 200) == pytest.approx(145)


def test_the_command_does_not_wait_for_the_arm_to_arrive():
    """**회귀 — 실기에서 못 쓸 정도로 느렸다.**

    도달을 2초 기다린 뒤 답하면 버튼 한 번에 UI 가 2초 잠긴다. 조그는 연타하는
    물건이라 그게 치명적이다.
    """
    import inspect

    from piper_robot.hub import RobotHub

    src = inspect.getsource(RobotHub.jog_end_pose)
    assert "time.sleep" not in src, "보내고 기다린다 — 화면이 그만큼 잠긴다"
    assert "_check_previous" in src, "그럼 도달 확인은 어디서 하나"


def test_pushing_the_same_bad_direction_twice_is_refused():
    """확인이 필요한 순간은 "못 가는 방향으로 **또** 미는" 때다 —
    그 순간이 바로 다음 명령이므로 거기서 보면 기다릴 필요가 없다."""
    from piper_robot.hub import RobotHub

    hub = RobotHub()
    hub._pending["can1"] = {
        "before": HOME, "target": dict(HOME, z=HOME["z"] + 5_000),
        "axis": "z", "delta": 5, "at": 0,      # 오래전 = 판정할 때가 됐다
    }
    # 팔이 그대로다 = 직전 명령이 못 갔다
    assert hub._check_previous("can1", HOME, "z", 5), "같은 방향인데 안 막는다"


def test_the_opposite_direction_is_still_allowed():
    """빠져나오는 방향까지 막으면 갇힌다 — 작업 공간 상자와 같은 규율."""
    from piper_robot.hub import RobotHub

    hub = RobotHub()
    pend = {"before": HOME, "target": dict(HOME, z=HOME["z"] + 5_000),
            "axis": "z", "delta": 5, "at": 0}
    hub._pending["can1"] = dict(pend)
    assert hub._check_previous("can1", HOME, "z", -5) is None, "반대 방향을 막는다"
    hub._pending["can1"] = dict(pend)
    assert hub._check_previous("can1", HOME, "x", 5) is None, "다른 축을 막는다"


def test_a_command_still_settling_is_not_judged():
    """보내자마자 판정하면 **가는 중인 것을 못 갔다고** 한다."""
    import time

    from piper_robot.hub import RobotHub

    hub = RobotHub()
    hub._pending["can1"] = {"before": HOME, "target": dict(HOME, z=HOME["z"] + 5_000),
                            "axis": "z", "delta": 5, "at": time.time()}
    assert hub._check_previous("can1", HOME, "z", 5) is None


def test_the_screen_watches_the_arm_while_it_moves():
    """안 하면 눌러도 화면이 가만히 있어 "안 먹었나" 싶어 또 누르게 된다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components"
           / "EndPosePanel.tsx").read_text()
    assert "track()" in src and "setInterval" in src
    assert "disabled={busy}" not in src, "버튼이 응답을 기다리며 잠긴다"
