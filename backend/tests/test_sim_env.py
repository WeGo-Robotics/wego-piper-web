"""시뮬레이션 환경 — 시뮬 Piper 가 실기 Piper 와 같은 숫자를 말하는가 (feature/sim-env.md).

시뮬레이터는 "또 하나의 로봇 데몬"이다. 여기서 지키는 것: 관절 규약이 실기
캘리브레이션과 일치, FK 가 `ArmModel` 과 일치(방향·오프셋 실수를 시뮬 켜기 전에
잡는다 — SO-101 릴레이 방향을 실기에서 뒤늦게 안 것의 재발 방지), 그리퍼
척도가 실기 0..68000µm 과 같은 뜻, 명령이 안전 필터를 지난다, 계약 동사.
"""

import math
from pathlib import Path

import numpy as np
import pytest

mujoco = pytest.importorskip("mujoco")
piper_sim = pytest.importorskip("piper_sim")

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def model():
    from piper_sim.scene import load_model
    return load_model()


def test_joint_ranges_match_the_piper_calibration_table(model):
    """시뮬 관절 범위 = 실기 캘리브레이션 표 (밀리도). 다르면 정규화 ±100 이
    다른 각도를 뜻하게 되어 실기 정책이 시뮬에서 다른 팔을 본다."""
    from piper_robot.joints import JOINT_CALIBRATION
    from piper_sim.scene import ARM_JOINTS

    for n in ARM_JOINTS:
        lo, hi = model.jnt_range[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, n)]
        clo, chi = JOINT_CALIBRATION[n]
        # joint5: URDF ±70° vs 표 ±65° — 표가 보수적이다. 표 범위가 URDF 안에 들면 된다.
        assert math.degrees(lo) * 1000 <= clo + 1 and math.degrees(hi) * 1000 >= chi - 1, n


def test_sim_fk_matches_the_kinematics_model_exactly(model):
    """⚠ **핵심 대조.** 같은 관절각에서 MuJoCo link6 자세 == ArmModel.fk.
    실측(2026-09-09): 6개 자세에서 0.00mm / 0.00° — 둘 다 같은 AgileX URDF 라
    당연하지만, 씬을 손볼 때(그리퍼·베이스 위치) 깨지면 여기서 잡힌다."""
    from piper_robot.armmodel import ArmModel
    from piper_sim.scene import JointMap

    am = ArmModel.load("piper")
    data = mujoco.MjData(model)
    jm = JointMap(model)
    rng = np.random.default_rng(1)
    for k in range(8):
        q = np.zeros(6) if k == 0 else rng.uniform(am.limits[:, 0] * 0.7, am.limits[:, 1] * 0.7)
        for i, n in enumerate(("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")):
            data.qpos[jm.qadr[n]] = q[i]
        mujoco.mj_forward(model, data)
        T = am.fk(q)
        assert np.linalg.norm(data.xpos[jm.link6] - T[:3, 3]) < 1e-6, f"위치 어긋남 q{k}"
        R = data.xmat[jm.link6].reshape(3, 3)
        assert abs(np.trace(R @ T[:3, :3].T) - 3) < 1e-6, f"자세 어긋남 q{k}"


def test_normalization_round_trips_through_the_sim_including_gripper(model):
    """정규화 → ctrl → (수렴했다 치고 qpos=ctrl) → 정규화 가 되돌아온다.
    그리퍼 100 = 두 손가락 합 68mm = 실기 68000µm 과 같은 뜻."""
    from piper_sim.scene import ARM_JOINTS, FINGER_STROKE_M, FINGERS, JointMap

    data = mujoco.MjData(model)
    jm = JointMap(model)
    goal = {"joint1": 30.0, "joint2": -50.0, "joint3": 10.0, "joint4": 0.0,
            "joint5": 80.0, "joint6": -20.0, "gripper": 100.0}
    jm.write_ctrl(data, goal)
    for n in ARM_JOINTS + FINGERS:
        data.qpos[jm.qadr[n]] = data.ctrl[jm.act[n]]
    back = jm.read_norm(data)
    for k, v in goal.items():
        assert back[k] == pytest.approx(v, abs=0.05), k
    assert data.ctrl[jm.act["gripper_l"]] == pytest.approx(FINGER_STROKE_M)


def test_commands_pass_the_real_safety_filter_and_deadman_holds():
    """명령은 `piper_robot.safety.filter_goal` 을 **그대로** 지난다 — 바닥·범위·
    변화율이 시뮬에서도 살아 필터 자체를 위험 없이 검증할 수 있다. 데드맨은
    그 자리에 서기(world.hold) 다 — 토크 차단이 아니다."""
    src = (REPO / "sim" / "piper_sim" / "bridge.py").read_text()
    assert "from piper_robot.safety import" in src and "filter_goal(now, values, self.safety" in src
    cmd = src.split("def _command_loop", 1)[1].split("\n    def ", 1)[0]
    assert "reader.is_stale()" in cmd and "self.world.hold()" in cmd


def test_the_daemon_speaks_the_contract_and_declares_sim_capabilities():
    src = (REPO / "daemons" / "simd.py").read_text()
    for verb in ("scan", "attach", "release", "estop", "info", "lost"):
        assert f'"{verb}"' in src, verb
    from piper_bus import contract as C
    assert C.SIMD in C.DAEMON_SOURCES
    hub = (REPO / "sim" / "piper_sim" / "hub.py").read_text()
    assert '"transport": "sim"' in hub and '"master_slave": False' in hub
    unit = (REPO / "deploy" / "systemd" / "piper-simd.service").read_text()
    assert "Restart=always" in unit and "MUJOCO_GL=egl" in unit


def test_the_scene_is_portable_and_is_the_only_thing_the_runtime_reads():
    """meshdir 이 절대 경로면 다른 기계에서 안 열린다. 런타임은 URDF 를 안 읽는다
    (build_arm_geometry 와 같은 규칙 — 배포 절차에 서브모듈 단계가 없다)."""
    xml = (REPO / "sim" / "piper_sim" / "assets" / "piper_scene.xml").read_text()
    md = xml.split('meshdir="', 1)[1].split('"', 1)[0]
    assert not md.startswith("/"), f"절대 meshdir: {md}"
    scene_py = (REPO / "sim" / "piper_sim" / "scene.py").read_text()
    assert "urdf" not in scene_py.lower()


def test_hold_latches_the_pose_once_instead_of_chasing_it():
    """⚠ **실측: 정지가 미끄러졌다** — 펼친 자세 hold 2초에 7.5 norm, 데드맨 뒤 0.5초에
    1.9. 루프가 hold 상태에서 **매 스텝** ctrl=qpos 로 다시 잡아, 서보가 처지는
    자세를 계속 따라갔기 때문이다. 래치는 한 번이어야 한다 — 그 뒤 ctrl 고정.
    고친 뒤: 처짐 0.07, 데드맨 뒤 1초 드리프트 0.0."""
    src = (REPO / "sim" / "piper_sim" / "world.py").read_text()
    loop = src.split("def _loop", 1)[1]
    latch = loop.split("self.jm.hold(self.data)", 1)[1][:120]
    assert "self._hold = False" in latch, "hold 가 매 스텝 재래치한다 — 정지가 미끄러진다"


# ── 2단계: 카메라 ──


def test_the_sim_camera_client_speaks_the_shared_hub_vocabulary():
    """세 허브(camerad·rsd·simd)가 한 어휘다 — 이름이 갈리면 호출부마다 분기가
    생긴다 (test_camerad_daemon 의 규칙을 세 번째 허브에도 건다)."""
    from app.services.sim_camera_client import sim_camera_hub
    for name in ("scan", "connect", "disconnect", "release_all", "probe",
                 "list_controls", "set_control", "has_frame", "get_jpeg",
                 "apply_controls", "lost", "last_apply_report", "info", "available"):
        assert callable(getattr(sim_camera_hub, name)), f"sim_camera_hub.{name} 없음"


def test_the_gateway_dispatches_sim_cameras_in_the_one_hub_branch():
    """분기는 `_hub()` 한 곳뿐이어야 한다 — 기존 규칙(realsense 리터럴 1회)을
    깨지 않고 sim 가지를 얹었는지."""
    src = (REPO / "backend" / "app" / "services" / "camera_manager.py").read_text()
    hub = src.split("def _hub", 1)[1].split("\n    def ", 1)[0]
    assert 'cam_type == "sim"' in hub and 'cam_type == "realsense"' in hub
    assert src.count('cam_type == "realsense"') == 1
    assert 'sim_camera_hub.scan()' in src, "스캔이 simd 카메라를 안 합친다"


def test_sim_controls_use_v4l2_names_so_profiles_and_lighting_apply():
    """컨트롤 이름이 v4l2 것이어야 `piper_cam.controls` 의 자동 모드 순서·단위가
    시뮬에도 걸린다 — 프로파일 적용·회색카드·조명 감시를 시뮬에서 그대로 시험하는
    조건이다. 적용기도 camerad 와 같은 함수다."""
    from piper_cam.controls import AUTO_SWITCHES, CONTROL_UNITS
    from piper_sim.cameras import _CONTROL_SPECS

    assert "auto_exposure" in _CONTROL_SPECS and "auto_exposure" in AUTO_SWITCHES
    assert "white_balance_automatic" in AUTO_SWITCHES
    assert "exposure_time_absolute" in CONTROL_UNITS and "exposure_time_absolute" in _CONTROL_SPECS
    src = (REPO / "sim" / "piper_sim" / "cameras.py").read_text()
    assert "controls_mod.apply_controls(" in src, "camerad 와 다른 적용기를 쓴다"


def test_emulation_scales_brightness_and_shifts_color_and_publishes_bgr():
    """노출×게인 = 밝기 배율, 색온도 = 채널 게인 (순수 함수). 그리고 세그먼트는
    **BGR** — yolod 가 RGB 로 오독해 채널을 뒤집었던 사고의 소비자 규약."""
    import numpy as np
    from piper_sim.cameras import emulate

    rgb = np.full((4, 4, 3), 100, dtype=np.uint8)
    auto = emulate(rgb, {"auto_exposure": 3})
    assert auto.mean() == 100
    dark = emulate(rgb, {"auto_exposure": 1, "exposure_time_absolute": 78, "gain": 64})
    assert dark.mean() == pytest.approx(50, abs=1), "노출 절반이면 밝기 절반"
    warm = emulate(rgb, {"white_balance_automatic": 0, "white_balance_temperature": 2800})
    assert warm[..., 0].mean() > warm[..., 2].mean(), "낮은 색온도는 붉어야 한다"
    src = (REPO / "sim" / "piper_sim" / "cameras.py").read_text()
    assert "out[..., ::-1]" in src and "BGR" in src


def test_one_render_thread_serves_all_cameras():
    """EGL 컨텍스트는 프로세스 전역이고 Renderer 는 스레드 안전이 아니다 —
    카메라마다 스레드를 띄우면 GL 이 깨진다. 렌더 스레드는 하나, update_scene 만
    세계 락 안에서."""
    src = (REPO / "sim" / "piper_sim" / "cameras.py").read_text()
    assert src.count("threading.Thread(") == 1
    # `def _render(` — 괄호까지 맞춘다. `_renderer(` 헬퍼가 접두사로 먼저 걸린다.
    render = src.split("def _render(", 1)[1].split("\n    def ", 1)[0]
    assert "with world._lock" in render and "r.update_scene" in render
    assert render.index("r.update_scene") < render.index("r.render()")
    unit = (REPO / "deploy" / "systemd" / "piper-simd.service").read_text()
    assert "MUJOCO_GL=egl" in unit


def test_probe_renders_on_the_render_thread_not_the_caller():
    """⚠ **실측: 5분에 EGLError 139회.** EGL 컨텍스트는 만든 스레드에 귀속된다 —
    probe 가 RPC 스레드에서 렌더러를 만들자 렌더 스레드가 매번 실패했다.
    렌더는 렌더 스레드만 한다: probe 는 요청 큐(`_ask`)로 부탁하고, `_render`
    를 직접 부르는 곳은 루프뿐이다."""
    src = (REPO / "sim" / "piper_sim" / "cameras.py").read_text()
    probe = src.split("def probe(", 1)[1].split("\n    def ", 1)[0]
    assert "self._ask(cam)" in probe and "self._render(" not in probe
    # _render 직접 호출은 루프(와 _ask 가 넘긴 잡 처리) 안에만
    outside = src.split("def _loop", 1)[0]
    assert outside.count("self._render(") == 0, "루프 밖에서 렌더한다"


def test_the_light_control_scales_the_headlight_too():
    """실측: 월드 라이트만 0.3배면 top 이 244→~225(포화), 헤드라이트까지 0.3배면
    244→122. 헤드라이트(카메라 부착 조명+앰비언트)가 씬 밝기의 절반이다 —
    그걸 빼면 조명 감시(급변·표류) 시험이 시뮬에서 성립하지 않는다."""
    src = (REPO / "sim" / "piper_sim" / "cameras.py").read_text()
    render = src.split("def _render(", 1)[1].split("\n    def ", 1)[0]
    assert "vis.headlight.diffuse" in render and "vis.headlight.ambient" in render
    assert "light_diffuse" in render


# ── 3단계: 등록 — SimArmInfo ──


def test_sim_arm_has_the_whole_arminfo_surface_without_robotd():
    """게이트웨이 25곳이 `robot_manager.arms[iface]` 의 ArmInfo 표면을 가정한다 —
    SimArmInfo 가 같은 표면을 지키면 그 25곳은 무수정이다. RPC 를 타는 메서드는
    전부 시뮬로 넘겨야 한다: 하나라도 robotd `_call` 로 새면 robotd 가 그 이름을
    몰라 조용히 실패한다."""
    import inspect

    from app.services.robot_manager import ArmInfo, SimArmInfo

    def _src(f) -> str:
        try:
            return inspect.getsource(f)
        except (OSError, TypeError):
            return ""        # dataclass 가 만든 __init__/__eq__ 등 — 소스가 없다

    rpc_methods = [n for n, f in vars(ArmInfo).items()
                   if callable(f) and not n.startswith("__") and "_call(" in _src(f)]
    assert rpc_methods, "ArmInfo 에 RPC 메서드가 없다? 앵커가 낡았다"
    for n in rpc_methods:
        assert n in vars(SimArmInfo), f"SimArmInfo 가 {n} 을 robotd 로 흘린다"
        body = inspect.getsource(vars(SimArmInfo)[n])
        assert "_call(" not in body or "sim.call(" in body, n
    a = SimArmInfo(iface="sim_follower1")
    assert a.transport == "sim" and a.to_dict()["transport"] == "sim"


def test_scan_merges_sim_arms_and_keeps_them_seen(monkeypatch):
    """⚠ `scan()` 은 `seen` 에 없는 팔을 mark_absent 한다. simd 가 보고한 팔을
    같은 스캔에서 seen 에 넣지 않으면 robotd 스캔 때마다 연결이 내려간다."""
    from app.services import robot_manager as rm
    from app.services import sim_robot_client as sim

    monkeypatch.setattr(rm, "_call", lambda method, *a, **k: [] if method == "scan" else None)
    monkeypatch.setattr(sim, "call", lambda method, *a, **k:
                        [{"id": "sim_follower1", "arm": "sim_follower1"}] if method == "scan" else None)
    mgr = rm.RobotManager()
    mgr.scan()
    arm = mgr.arms["sim_follower1"]
    assert isinstance(arm, rm.SimArmInfo) and arm.connected, "스캔이 sim 팔을 없음 처리했다"
    mgr.scan()
    assert mgr.arms["sim_follower1"].connected, "두 번째 스캔에서 없음 처리됐다"


def test_the_session_and_ports_and_page_know_the_transport():
    """세션이 transport 를 남겨야 복원이 클래스를 가르고, /ports 는 sim 을 CAN 통계
    루프에서 빼 자기 카드로 내며, 페이지는 sim 팔에서 마스터/슬레이브·0x150 을
    안 그린다 (capabilities — 시뮬에 없는 기능은 없다고 선언)."""
    rm_src = (REPO / "backend" / "app" / "services" / "robot_manager.py").read_text()
    assert '"transport": arm.transport' in rm_src
    router = (REPO / "backend" / "app" / "routers" / "robots.py").read_text()
    ports = router.split('"/ports"', 1)[1].split("@router.post", 1)[0]
    assert '"sim": sim_ports' in ports and 'transport", "can") == "sim"' in ports
    page = (REPO / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    assert "simPorts.map((sp) =>" in page
    assert "arm.transport !== 'sim' && <>" in page, "sim 팔에 마스터/슬레이브·리셋이 그려진다"


def test_a_stale_can_entry_for_a_sim_arm_is_upgraded_keeping_its_registration(monkeypatch):
    """⚠ **실측: 옛 세션이 sim_follower1 을 `transport: can` 으로 굳혀 둬** 포트
    카드가 CAN 쪽에 섞이고 raw 읽기가 robotd 로 새어 503 이 났다. 스캔은 기존
    항목을 교체하지 않고 **승격**한다(role/side/ready 유지) — simd 가 아직 안
    떠도 `sim_` 접두사면 시뮬 팔이다."""
    from app.services import robot_manager as rm
    from app.services import sim_robot_client as sim

    monkeypatch.setattr(rm, "_call", lambda method, *a, **k: [] if method == "scan" else None)
    monkeypatch.setattr(sim, "call", lambda method, *a, **k: [])      # simd 죽어 있음
    mgr = rm.RobotManager()
    stale = rm.ArmInfo(iface="sim_follower1")
    stale.role, stale.side, stale.ready = "follower", "left", True
    mgr.arms["sim_follower1"] = stale
    mgr.scan()
    arm = mgr.arms["sim_follower1"]
    assert isinstance(arm, rm.SimArmInfo) and arm.transport == "sim"
    assert (arm.role, arm.side, arm.ready) == ("follower", "left", True), "승격이 등록을 날렸다"


def test_the_raw_joint_route_reads_sim_arms_through_the_arm_object():
    """`/joints/raw` 는 ArmInfo 표면을 우회해 robotd 로 직행하던 유일한 읽기 경로다
    — 시뮬 팔에서 503 이 났다(실측). sim 이면 팔 객체(shm)로 읽어 같은 모양
    (밀리도 dict)으로 돌려준다. 나머지 직접 RPC(진단·버스 통계)는 CAN 전용이라
    그대로다."""
    router = (REPO / "backend" / "app" / "routers" / "robots.py").read_text()
    body = router.split('@router.get("/joints/raw/{iface}")', 1)[1].split("@router.", 1)[0]
    assert 'transport", "can") == "sim"' in body
    assert "arm.read_joints_normalized()" in body and "denormalize_all(norm)" in body
    assert body.index('== "sim"') < body.index('_call("read_raw_all"'), "sim 분기가 robotd 호출 뒤에 있다"
