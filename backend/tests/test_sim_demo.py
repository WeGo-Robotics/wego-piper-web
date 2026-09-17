"""스크립트 시연 — 사람 없이 에피소드를 만든다 (feature/sim-env.md 4단계).

이 파일의 **인수 기준은 하나**다: 시연 궤적을 실제 세계에 돌리면 큐브가 통 안에 들어간다.
계획만 맞고 물리가 안 따라오면 그건 데이터가 아니라 낭비다.

시연은 웹 리더와 **같은 자리**에 선다 — 리더 세그먼트를 발행하고, 수집은 그걸 읽는다.
그래서 수집·추론·E-stop 경로가 하나도 안 바뀐다.
"""

import numpy as np
import pytest

pytest.importorskip("piper_sim")
mujoco = pytest.importorskip("mujoco")


def _run_plan(world, model, R, plan_rows, seed_deg, hz=30):
    """계획을 세계에 **실제로 돌린다** — 발행 대신 팔로워 목표로 바로 쓴다(shm 없이)."""
    from app.services.relay import _norm_from_rad

    seed = np.radians(seed_deg)
    grip = 100.0
    dt = world.model.opt.timestep
    for _name, p, want, secs in plan_rows:
        T = np.eye(4)
        T[:3, :3], T[:3, 3] = R, p
        sol = model.ik(T, seed)
        assert sol.ok, f"{_name} 구간을 못 푼다"
        steps = int(secs * hz)
        for i in range(1, steps + 1):
            a = i / steps
            pose = _norm_from_rad(seed + (sol.q - seed) * a)
            pose["gripper"] = grip + (want - grip) * a
            for _ in range(int((1 / hz) / dt)):
                world.jm.write_ctrl(world.data, pose)
                mujoco.mj_step(world.model, world.data)
        seed, grip = sol.q, want
    return seed


@pytest.fixture(scope="module")
def demo():
    from app.services import sim_demo as D

    return D


def test_the_planned_trajectory_stays_inside_what_the_real_arm_can_do(demo):
    """⚠ **URDF 한계가 아니라 캘리브레이션 한계**로 푼다. `ArmModel` 은 URDF 를 쓰는데
    (joint5 ±75°) 실기 표는 ±65° 다 — 그 사이에서 IK 가 해를 고르면 정규화가 ±100 을 넘고
    `denormalize_all` 이 잘라서 팔이 명령한 자세에 **영영 못 간다**. 실측: 그렇게 만든
    궤적은 큐브를 집는 대신 밀어냈다(z 가 0.02 에서 안 올라갔다).

    시뮬 데이터가 실기에서 재생돼야 한다는 것이 이 기획의 전제이므로 시뮬 쪽을 좁힌다."""
    from app.services.relay import _norm_from_rad
    from piper_robot.joints import JOINT_CALIBRATION

    m = demo._model()
    for i, j in enumerate(demo.JOINTS):
        lo, hi = JOINT_CALIBRATION[j]
        assert np.degrees(m.limits[i, 0]) >= lo / 1000 - 1e-6
        assert np.degrees(m.limits[i, 1]) <= hi / 1000 + 1e-6

    R = demo.grasp_rotation(m)
    seed = np.radians(demo.GRASP_SEED_DEG)
    for name, p, _g, _s in demo.plan([0.35, 0.0], [0.35, -0.25]):
        T = np.eye(4)
        T[:3, :3], T[:3, 3] = R, p
        sol = m.ik(T, seed)
        assert sol.ok, f"{name} 을 못 푼다 ({sol.pos_mm:.0f}mm)"
        over = {k: v for k, v in _norm_from_rad(sol.q).items() if abs(v) > 100}
        assert not over, f"{name} 에서 정규화 범위를 넘는다: {over} — 실기는 거기 못 간다"
        seed = sol.q


def test_the_grasp_pose_really_points_down_and_the_fingers_reach_the_table(demo):
    """높이는 실측에서 온다. `fk` 는 **link6** 프레임이고(URDF 에 그리퍼가 없다) 손가락 끝은
    툴 +z 로 0.090m 앞이다 — 그래서 파지 높이가 0.095 다(손가락 끝 z≈0.005)."""
    m = demo._model()
    T = m.fk(np.radians(demo.GRASP_SEED_DEG))
    assert T[2, 2] < -0.97, f"툴 z 가 아래를 안 본다: {T[:3, 2]}"
    assert demo.FINGER_REACH == pytest.approx(0.090, abs=0.002)
    assert demo.GRASP_Z - demo.FINGER_REACH == pytest.approx(0.005, abs=0.002), \
        "파지 높이가 손가락 길이와 안 맞는다 — 큐브를 밀거나 허공을 쥔다"


def test_the_demo_names_no_object_so_it_runs_in_a_scene_people_made(demo):
    """장면은 사람이 만들고 id 도 사람이 짓는다 — `cube`·`bin` 을 박으면 자기 장면에서
    시연이 안 돈다. **움직이는 것을 집어 고정물에 넣는다**로 고른다."""
    objs = [{"id": "통", "movable": False, "pos": [0.35, -0.25, 0]},
            {"id": "내블럭", "movable": True, "pos": [0.35, 0, 0.02]}]
    pick, place = demo.SimDemo._pick_objects(objs)
    assert pick["id"] == "내블럭" and place["id"] == "통"
    with pytest.raises(demo.DemoError, match="집을 물체가 없습니다"):
        demo.SimDemo._pick_objects([objs[0]])
    with pytest.raises(demo.DemoError, match="넣을 곳이 없습니다"):
        demo.SimDemo._pick_objects([objs[1]])


def test_a_scene_the_arm_cannot_reach_is_refused_before_the_episode_is_wasted(demo):
    """통이 y=−0.30 이면 안 닿는다(실측). 돌려 보고 알면 그 에피소드는 버리는 데이터다."""
    assert demo.check_reach([0.35, 0.0], [0.35, -0.25]) == []
    bad = demo.check_reach([0.35, 0.0], [0.35, -0.36])
    assert bad and "모자람" in bad[0]


def test_the_demo_actually_puts_the_cube_in_the_bin(demo):
    """**이 기획의 인수 기준.** 계획이 풀리는 것과 물리가 따라오는 것은 다른 일이다 —
    첫 궤적은 IK 가 전부 ok 였는데도 큐브를 밀기만 했다(정규화 범위를 넘어서).

    실측(2026-09-17): 큐브가 (0.35, 0.00) 에서 통 (0.35, −0.25) 안 (0.353, −0.250, 0.026) 으로.
    """
    from piper_sim.world import World

    w = World()
    m = demo._model()
    R = demo.grasp_rotation(m)
    cube, target = w.object_pos("cube"), w.object_pos("bin")
    _run_plan(w, m, R, demo.plan(cube[:2], target[:2]), demo.GRASP_SEED_DEG)
    final = w.object_pos("cube")
    assert abs(final[0] - target[0]) < 0.08 and abs(final[1] - target[1]) < 0.08, \
        f"큐브가 통 위가 아니다: {np.round(final, 3)}"
    assert final[2] < 0.08, f"큐브를 통에 안 떨어뜨렸다: z={final[2]:.3f}"


def test_the_demo_writes_the_same_leader_the_teleop_window_does(demo):
    """수집은 `piper_leader_shm` 으로 리더 세그먼트를 읽을 뿐 **누가 쓰는지는 안 본다** —
    조종 창이든 시연이든 게이트웨이가 리더 노릇을 하는 것은 같다.

    ⚠ 처음엔 `sim_leader1` 로 따로 뒀다가 되돌렸다. 따로 두면 수집 화면에 리더 종류가 하나
    더 생기고 사람이 매번 둘 중 무엇인지 판단해야 한다. 이름을 나눈 근거("둘이 같이 쓰면
    누가 민 자세인지 모른다")는 **동시에 못 돌게** 하면 사라지고, 그 가드는 양쪽에 있어야
    한다 — 한쪽만 막으면 시연 중에 조종 창을 열어 세그먼트를 뺏을 수 있다."""
    from app.services.web_leader import LEADER_NAME as WEB

    assert demo.LEADER_NAME == WEB, "리더 종류가 둘로 갈렸다 — 수집 화면이 하나 더 묻게 된다"
    wl = (__import__("pathlib").Path(__file__).resolve().parents[2]
          / "backend" / "app" / "services" / "web_leader.py").read_text()
    assert "sim_demo.is_running" in wl, "시연이 도는데 조종 창이 같은 세그먼트를 뺏는다"
    demo_src = (__import__("pathlib").Path(__file__).resolve().parents[2]
                / "backend" / "app" / "services" / "sim_demo.py").read_text()
    assert "web_leader.is_running" in demo_src, "조종 창이 잡고 있는데 시연이 끼어든다"
    src = (__import__("pathlib").Path(__file__).resolve().parents[2]
           / "backend" / "app" / "services" / "sim_demo.py").read_text()
    assert "StateWriter(LEADER_NAME)" in src, "리더 세그먼트를 발행하지 않는다"
    assert "relay_session.start(LEADER_NAME, FOLLOWER" in src, "릴레이를 못 켠다"
    # 수집 중엔 릴레이를 끈다 — 녹화 프로세스와 릴레이가 같은 팔로워 세그먼트를 못 쥔다
    router = (__import__("pathlib").Path(__file__).resolve().parents[2]
              / "backend" / "app" / "routers" / "sim_demo.py").read_text()
    assert "relay: bool = True" in router and "녹화 프로세스가 팔로워를 쥔다" in router
