"""웹 리더 — 키보드·마우스가 가리키는 자세를 가상 리더 세그먼트로 (feature/web-leader.md).

브라우저가 아니라 게이트웨이가 적분한다. 입력이 100ms 끊기면 속도 0. EE 는 릴레이
POSE 와 같은 IK 이고 못 풀면 목표를 되돌린다. 팔로워는 릴레이가 움직인다 — 새 경로가 없다.
"""

from pathlib import Path

import numpy as np
import pytest

from app.services import web_leader as W

REPO = Path(__file__).resolve().parents[2]
HOME = {f"joint{i}": 0.0 for i in range(1, 7)} | {"gripper": 50.0}


def _run(integ, secs, dt=1 / 30, **feed):
    t = 100.0
    for _ in range(int(secs / dt)):
        if feed:
            integ.feed(t, **feed)
        integ.step(t, dt); t += dt
    return integ.pose, t


def test_a_held_key_moves_one_joint_at_the_set_speed_and_letting_go_stops_it():
    integ = W.Integrator(HOME)
    pose, t = _run(integ, 1.0, keys=["q"])
    assert pose["joint1"] == pytest.approx(W.JOINT_SPEED, abs=1.5)
    assert all(pose[j] == 0.0 for j in W.JOINTS if j != "joint1")
    # 입력이 끊기면(100ms) 속도 0 — 키를 "놓지" 않았어도 팔은 선다
    before = pose["joint1"]
    for _ in range(15):
        integ.step(t, 1 / 30); t += 1 / 30
    assert integ.pose["joint1"] == pytest.approx(before, abs=W.JOINT_SPEED * 0.12)
    pose, _ = _run(W.Integrator(HOME), 1.0, keys=["a"], shift=True)
    assert pose["joint1"] == pytest.approx(-W.JOINT_SPEED * W.JOINT_FAST, abs=4)
    pose, _ = _run(W.Integrator(HOME), 10.0, keys=["w"], shift=True)
    assert pose["joint2"] == 100.0, "범위를 넘지 않는다"


def test_gripper_buttons_wheel_and_snap_clicks():
    integ = W.Integrator(HOME)
    pose, _ = _run(integ, 0.5, mouse={"buttons": [0]})          # 왼쪽 누르는 동안 닫힌다
    assert pose["gripper"] == pytest.approx(50 - W.GRIPPER_SPEED * 0.5, abs=3)
    integ.feed(200.0, keys=[], click="open"); integ.step(200.0, 1 / 30)
    assert integ.pose["gripper"] == 100.0
    integ.feed(201.0, keys=[], select="joint3"); integ.step(201.0, 1 / 30)
    integ.feed(201.1, keys=[], mouse={"wheel": -3}); integ.step(201.1, 1 / 30)
    assert integ.pose["joint3"] == pytest.approx(3 * W.WHEEL_JOINT)
    integ.feed(202.0, keys=[], mouse={"dx": 150, "dy": -150, "buttons": [2]}); integ.step(202.0, 1 / 30)
    assert integ.pose["joint1"] == pytest.approx(10.0) and integ.pose["joint2"] == pytest.approx(10.0)


class _FakeModel:
    """IK 를 흉내 낸다 — 목표 x 를 joint2 로 바꾼 것처럼. 실패 조건은 테스트가 정한다."""
    def __init__(self):
        self.fail = False; self.jump = False; self.low = 1.0
    def fk(self, q):
        T = np.eye(4); T[:3, 3] = [0.3 + q[1], q[0], 0.2]; return T        # 1 m / rad — 작은 걸음이 작은 각
    def ik(self, T, seed):
        q = seed.copy(); q[1] = T[0, 3] - 0.3; q[0] = T[1, 3]
        if self.jump:
            q[3] = seed[3] + np.radians(60)
        class S: ok = not self.fail; iters = 3; pos_mm = 40.0 if self.fail else 0.1; rot_deg = 0.1
        S.q = q; return S
    def lowest_z(self, q):
        return self.low


def test_ee_mode_integrates_mouse_into_the_target_and_reverts_when_ik_refuses(monkeypatch):
    integ = W.Integrator(HOME)
    fake = _FakeModel(); integ.model = fake
    monkeypatch.setattr("app.services.relay._norm_from_rad", lambda q: {f"joint{i + 1}": float(np.degrees(q[i])) for i in range(6)})
    integ.set_mode("ee")
    x0 = integ.T[0, 3]
    integ.feed(300.0, keys=[], mouse={"dy": -300}); integ.step(300.0, 1 / 30)     # 위로 300px = 앞으로 6cm
    assert integ.T[0, 3] == pytest.approx(x0 + 0.06, abs=1e-6) and integ.blocked == ""
    assert integ.pose["joint2"] != 0.0, "IK 해가 자세에 반영된다"
    integ.feed(300.1, keys=[], mouse={"wheel": 2}); integ.step(300.1, 1 / 30)      # 휠 아래 = z 내림
    assert integ.T[2, 3] == pytest.approx(0.2 - 2 * W.WHEEL_Z, abs=1e-6)
    R0 = integ.T[:3, :3].copy()
    integ.feed(300.2, keys=["e"]); integ.step(300.2, 1 / 30)   # e = 요(회전키) — 마우스 없이도 회전
    assert not np.allclose(integ.T[:3, :3], R0)
    fake.fail = True; T_before = integ.T.copy()
    integ.feed(300.3, keys=["w"]); integ.step(300.3, 1 / 30)   # w = 피치
    assert np.allclose(integ.T, T_before) and "도달 불가" in integ.blocked
    fake.fail = False; fake.jump = True
    integ.feed(300.4, keys=["w"]); integ.step(300.4, 1 / 30)
    assert np.allclose(integ.T, T_before) and "막혀" in integ.blocked
    fake.jump = False; fake.low = -0.05; integ.floor_cm = -4.0
    integ.feed(300.5, keys=[], mouse={"dy": -30}); integ.step(300.5, 1 / 30)   # 이동으로 바닥 아래
    assert np.allclose(integ.T, T_before) and "바닥" in integ.blocked
    integ.pose_lock = True; fake.low = 1.0
    integ.feed(300.6, keys=["e"]); integ.step(300.6, 1 / 30)                          # 자세 고정: 회전키 무시
    assert np.allclose(integ.T, T_before)


def test_start_publishes_the_anchor_then_relays_with_the_web_leader_name(monkeypatch):
    from app.services import web_leader as svc
    published, started, stopped = [], [], []
    class FakeReader:
        def __init__(self, iface): self.iface = iface
        def read(self): return {"values": dict(HOME)}
        def close(self): pass
    class FakeWriter:
        def __init__(self, name): self.name = name; published.append(name)
        def publish(self, values): published.append(dict(values)); return 1
        def close(self): stopped.append("closed")
    from piper_shm import arm as shm_arm
    monkeypatch.setattr(shm_arm, "StateReader", FakeReader)
    monkeypatch.setattr(shm_arm, "StateWriter", FakeWriter)
    monkeypatch.setattr("app.services.robot_manager._call", lambda *a, **k: {"enabled": True, "min_z_cm": -4})
    from app.services import relay
    class FakeRelay:
        is_running = False
        def start(self, leader, follower, mode, la, fa): started.append((leader, follower, mode, la, fa)); self.is_running = True
        def stop(self): stopped.append("relay"); self.is_running = False
        def status(self): return {"leader": svc.LEADER_NAME, "running": self.is_running, "holding": True, "sent": 3, "stale": False, "blocked": ""}
    monkeypatch.setattr(relay, "relay_session", FakeRelay())
    wl = svc.WebLeader()
    st = wl.start("sim_follower1")
    try:
        assert published[0] == svc.LEADER_NAME and published[1] == HOME, "릴레이가 읽을 앵커를 먼저 발행한다"
        assert started == [(svc.LEADER_NAME, "sim_follower1", "joint", "piper", "piper")]
        assert st["running"] and st["follower"] == "sim_follower1" and wl.integ.floor_cm == -4.0
        with pytest.raises(RuntimeError, match="이미"):
            wl.start("can0")
        wl.input(keys=["q"], mouse={}, shift=False, ctrl=False)
    finally:
        wl.stop()
    assert "relay" in stopped and "closed" in stopped and not wl.is_running
    with pytest.raises(RuntimeError, match="조종 중이 아닙니다"):
        wl.input(keys=[])


def test_the_window_owns_the_input_and_lets_go_when_it_loses_the_user():
    """별도 창 — 포인터 락은 클릭으로, 창 닫힘은 beacon 으로 stop, 포커스 잃음은 정지 상태,
    heartbeat 도 이 창이, 우클릭 메뉴는 막는다. 첫 클릭은 그리퍼가 아니다."""
    src = (REPO / "frontend" / "src" / "pages" / "TeleopWindowPage.tsx").read_text()
    for needle in ("requestPointerLock()", "pointerlockchange", "'pagehide'", "sendBeacon('/api/leader/web/stop'",
                   "addEventListener('blur'", "visibilitychange", "/estop/heartbeat", "'contextmenu'",
                   "/leader/web/input", "e.code === 'Tab'", "e.code === 'Space'", "held < 150", "/preview?t="):
        assert needle in src, needle
    assert "window.confirm(" not in src
    pages = (REPO / "frontend" / "src" / "config" / "pages.ts").read_text()
    assert "{ path: '/teleop', label: '조종 창', component: TeleopWindowPage, nav: false, standalone: true }" in pages
    robots = (REPO / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    assert "window.open(`/teleop?follower=${encodeURIComponent(arm.iface)}`, 'piper-teleop'" in robots, "이름 고정 창 — 둘이 한 팔을 조종하지 않는다"
    router = (REPO / "backend" / "app" / "routers" / "web_leader.py").read_text()
    for path in ('"/start"', '"/input"', '"/stop"', '"/status"'):
        assert path in router


def test_recording_takes_the_follower_from_the_relay_and_the_leader_keeps_publishing(monkeypatch):
    """수집 중엔 녹화 프로세스가 팔로워 명령 세그먼트를 쥔다 — 릴레이가 같이 쥘 수 없다.
    그래서 웹 리더는 발행만 하고, 녹화가 piper_leader_shm 으로 세그먼트를 읽는다."""
    from app.services import web_leader as svc
    class FakeReader:
        def __init__(self, iface): pass
        def read(self): return {"values": dict(HOME)}
        def close(self): pass
    class FakeWriter:
        def __init__(self, name): self.pub = 0
        def publish(self, values): self.pub += 1; return self.pub
        def close(self): pass
    from piper_shm import arm as shm_arm
    monkeypatch.setattr(shm_arm, "StateReader", FakeReader); monkeypatch.setattr(shm_arm, "StateWriter", FakeWriter)
    monkeypatch.setattr("app.services.robot_manager._call", lambda *a, **k: {})
    from app.services import relay
    calls = []
    class FakeRelay:
        is_running = False; holding = False
        def start(self, *a): calls.append("start"); self.is_running = self.holding = True
        def stop(self): calls.append("stop"); self.is_running = self.holding = False
        def status(self): return {"leader": svc.LEADER_NAME if self.is_running else None, "running": self.is_running, "holding": self.holding, "sent": 0, "stale": False, "blocked": ""}
    monkeypatch.setattr(relay, "relay_session", FakeRelay())
    wl = svc.WebLeader()
    try:
        wl.start("can0")                                   # 실기: 속도 절반
        assert wl.integ.speed_scale == 0.5 and wl.status()["relaying"] is True
        assert wl.release_follower() is True and calls == ["start", "stop"]
        assert wl.is_running and wl.status()["publishing"] and wl.status()["relaying"] is False
        assert wl.release_follower() is False
    finally:
        wl.stop()
    wl2 = svc.WebLeader()
    try:
        wl2.start("sim_follower1", relay=False)            # 수집이 먼저 시작한 경우
        assert calls == ["start", "stop"] and wl2.integ.speed_scale == 1.0 and wl2.status()["relaying"] is False
    finally:
        wl2.stop()
    src = (REPO / "backend" / "app" / "routers" / "recording.py").read_text()
    head = src.split("async def start_recording", 1)[1]
    assert head.index("web_leader.release_follower") < head.index("require_idle(Activity.RECORDING)"), \
        "릴레이가 수동 조작 잠금을 쥔 채면 require_idle 이 먼저 막는다 — 놓는 게 먼저다"
    assert 'web_leader.start, body.robot_port, "joint", False' in src
    page = (REPO / "frontend" / "src" / "pages" / "RecordingPage.tsx").read_text()
    assert '<option value="web_leader1">웹 리더 (키보드·마우스)</option>' in page and "'piper-teleop'" in page
    wl_router = (REPO / "backend" / "app" / "routers" / "web_leader.py").read_text()
    assert "ex.is_running(ex.Activity.RECORDING)" in wl_router and "not recording" in wl_router


def test_environment_reset_ramps_the_arm_home_and_only_teleports_the_cube(monkeypatch):
    """환경 리셋 — 큐브만 시작 위치로 텔레포트, **팔은 램프로 복귀**(순간이동 아님). 조종
    중이면 리더 자세를 파킹으로 램프해 팔로워가 따라오고 수집엔 복귀가 액션으로 기록된다.
    리더가 없으면 데몬 go_to 가 램프한다. 실기 팔은 거절."""
    from app.services import web_leader as svc, sim_robot_client as sim
    import pytest
    calls = []
    monkeypatch.setattr(sim, "call", lambda m, *a, default=None, **k: calls.append((m, a)) or default)
    # 조종 중이 아닐 때: 큐브 텔레포트 + 데몬 go_to 램프
    svc.web_leader.integ = None; svc.web_leader._writer = None; svc.web_leader.follower = None
    r = svc.reset_sim_world("sim_follower1")
    assert r["homing"] is True and ("reset_cube", ()) in calls
    assert any(c[0] == "go_to" and c[1][0] == "sim_follower1" and c[1][1] == svc.PARKING for c in calls), "리더 없으면 데몬이 램프"
    with pytest.raises(RuntimeError, match="실기"):
        svc.reset_sim_world("can0")
    # 조종 중이면 리더가 **램프**로 파킹으로 (즉시 스냅 아님)
    integ = svc.Integrator({f"joint{i}": 50.0 for i in range(1, 7)} | {"gripper": 80.0})
    svc.web_leader.integ = integ; svc.web_leader._writer = object(); svc.web_leader.follower = "sim_follower1"
    try:
        svc.reset_sim_world("sim_follower1", arm_only=True)
        assert integ.homing == svc.PARKING, "리더가 램프 목표(파킹)를 안 잡았다"
        assert integ.pose["joint1"] == 50.0, "복귀가 순간이동이면 안 된다 — 램프여야 한다"
        # 스텝을 돌리면 서서히 다가간다
        for _ in range(200):
            integ.step(0, 1/30)
        assert abs(integ.pose["joint2"] - svc.PARKING["joint2"]) < 1.0 and integ.homing is None
    finally:
        svc.web_leader.integ = None; svc.web_leader._writer = None; svc.web_leader.follower = None
    assert "def start_homing" in (REPO / "backend" / "app" / "services" / "web_leader.py").read_text()
    win = (REPO / "frontend" / "src" / "pages" / "TeleopWindowPage.tsx").read_text()
    assert "/leader/web/reset" in win and "환경 리셋" in win and "e.code === 'KeyR'" in win



def test_ee_rotation_keys_and_mouse_move_the_pose_together(monkeypatch):
    """EE 는 마우스=이동, QWEASD=회전 → 드래그하며 키를 눌러 6D 를 동시에 움직인다
    (사용자 결정 2026). q/a=roll, w/s=pitch, e/d=yaw."""
    import numpy as np
    from app.services import web_leader as W
    assert W.EE_KEYS == {"q": ("roll", 1), "a": ("roll", -1), "w": ("pitch", 1),
                         "s": ("pitch", -1), "e": ("yaw", 1), "d": ("yaw", -1)}
    integ = W.Integrator(HOME)
    fake = _FakeModel(); integ.model = fake
    monkeypatch.setattr("app.services.relay._norm_from_rad", lambda q: {f"joint{i+1}": float(np.degrees(q[i])) for i in range(6)})
    integ.set_mode("ee")
    x0 = integ.T[0, 3]; R0 = integ.T[:3, :3].copy()
    # 마우스 앞으로 + w(pitch) 동시
    integ.feed(400.0, keys=["w"], mouse={"dy": -150}); integ.step(400.0, 1/30)
    assert integ.T[0, 3] > x0 + 0.02, "마우스 이동이 안 먹었다"
    assert not np.allclose(integ.T[:3, :3], R0), "키 회전이 안 먹었다 (동시 6D)"


def test_t_key_resets_only_the_arm_not_the_cube(monkeypatch):
    """T = 로봇 위치만 초기화 — 큐브·조명은 그대로 (사용자 요청 2026)."""
    from app.services import web_leader as svc, sim_robot_client as sim
    seen = []
    monkeypatch.setattr(sim, "call", lambda m, *a, default=None, **k: seen.append((m, a)) or default)
    svc.web_leader.integ = None; svc.web_leader._writer = None; svc.web_leader.follower = None
    r = svc.reset_sim_world("sim_follower1", arm_only=True)
    assert r["arm_only"] is True and not any(c[0] == "reset_cube" for c in seen), "T(팔만)인데 큐브를 건드렸다"
    seen.clear()
    svc.reset_sim_world("sim_follower1", arm_only=False)
    assert any(c[0] == "reset_cube" for c in seen), "환경 리셋은 큐브를 시작 위치로"
    # 창: T 는 KEY_OF 로 안 가고 팔 리셋, EE 도움말은 QWEASD 회전
    win = (REPO / "frontend" / "src" / "pages" / "TeleopWindowPage.tsx").read_text()
    assert "e.code === 'KeyT'" in win and "resetWorld(true)" in win
    assert "KeyT:" not in win, "T 가 KEY_OF 에 남아 관절 모드로 샌다"
    assert "롤 (+/−)" in win and "피치 (+/−)" in win and "요 (+/−)" in win, "EE 회전 키가 도움말에 없다"


def test_ee_resyncs_to_the_real_arm_when_they_diverge_so_it_never_gets_stuck(monkeypatch):
    """⚠ **사용자 보고(2026)**: 집다 먹통 → 로봇 초기화 → 그 뒤 마우스가 어디로도 못 감.
    EE 는 목표 T·시드 q 를 개루프로 누적하는데, 팔이 밖에서 리셋되면 통합기 믿음이
    실제와 어긋나 매 스텝 되돌려 영영 막힌다. 실제와 크게 벌어지면 실제로 재동기한다."""
    import numpy as np
    from app.services import web_leader as W
    integ = W.Integrator(HOME); integ.model = _FakeModel()
    monkeypatch.setattr("app.services.relay._norm_from_rad", lambda q: {f"joint{i+1}": float(np.degrees(q[i])) for i in range(6)})
    integ.set_mode("ee")
    # 팔을 멀리 옮겨 놓았다고 치고 몇 번 이동 (통합기 T 가 그쪽으로 누적)
    for _ in range(5):
        integ.feed(0, keys=[], mouse={"dy": -30}); integ.step(0, 1/30)
    far_T = integ.T.copy(); far_q = integ.q_rad.copy()
    # 밖에서 팔을 파킹으로 리셋 — 통합기는 아직 far_q 를 믿는다
    parking_q = np.zeros(6)
    integ.ee_resync(parking_q)
    assert not np.allclose(integ.q_rad, far_q), "실제와 벌어졌는데 재동기 안 됨 — 막힌 채로 남는다"
    assert np.allclose(integ.q_rad, parking_q) and integ.blocked == ""
    # 재동기 뒤 이동이 실제로 먹는다 (막히지 않음)
    z0 = integ.T[2, 3]
    integ.feed(0, keys=[], mouse={"wheel": -1}); integ.step(0, 1/30)
    assert integ.T[2, 3] != z0 and integ.blocked == ""
    # 작은 차이(추종 지연)면 재동기 안 한다 — 매 틱 튀지 않게
    integ.ee_resync(integ.q_rad + np.radians(2))
    assert np.allclose(integ.q_rad, parking_q, atol=1e-9), "작은 추종 지연에도 스냅하면 조작이 튄다"
    # 루프가 EE 모드에서 매 틱 재동기하는가 (소스 대조)
    src = (REPO / "backend" / "app" / "services" / "web_leader.py").read_text()
    loop = src.split("def _loop", 1)[1]
    assert "integ.observe(_follower_norm(reader))" in loop



def test_ee_seeds_ik_from_the_real_arm_so_an_obstacle_holds_the_target_at_the_arm():
    """시뮬이 장애물에 막혀 관절이 안 움직이면, IK 시드를 실제 관절로 잡아 목표가 실제보다
    앞서 달아나지 못하게 한다(사용자 지적 2026). 막힌 팔에서 계속 밀면 step 이 커져 상한에
    걸리고 목표는 실제에 붙잡힌다 — 풀리면 이어진다."""
    import numpy as np
    from app.services import web_leader as W

    class SteepModel:                       # 1cm 이동 = 큰 관절 변화 → 상한이 실제처럼 걸린다
        def fk(self, q):
            T = np.eye(4); T[:3, 3] = [0.3 + q[1] * 0.02, q[0] * 0.02, 0.2]; return T
        def ik(self, T, seed):
            q = seed.copy(); q[1] = (T[0, 3] - 0.3) / 0.02; q[0] = T[1, 3] / 0.02
            class S: ok = True; iters = 3; pos_mm = 0.1; rot_deg = 0.1
            S.q = q; return S
        def lowest_z(self, q): return 1.0
    integ = W.Integrator(HOME); integ.model = SteepModel()
    integ.set_mode("ee")
    stuck_norm = {f"joint{i+1}": 0.0 for i in range(6)} | {"gripper": 50.0}   # HOME 에 막혀 있다
    T0 = integ.T.copy(); blocked_seen = False
    for _ in range(20):
        integ.observe(stuck_norm)                 # 실제는 계속 HOME(막힘)
        integ.feed(0, keys=[], mouse={"dy": -300}); integ.step(0, 1/30)   # 세게 민다
        blocked_seen = blocked_seen or ("막혀" in integ.blocked)
    adv = float(integ.T[0, 3] - T0[0, 3])
    # 막힌 팔에서 계속 밀어도 목표는 상한(≈0.9cm)에서 멈춘다 — 무한히 달아나지 않는다
    assert adv < 0.02, f"막힌 팔인데 목표가 계속 달아났다: {adv*100:.1f}cm"
    assert blocked_seen, "막혔는데 사용자에게 아무 말도 안 했다"


def test_joint_mode_rebases_the_command_to_the_real_arm_when_blocked_or_reset():
    """관절 모드도 명령 자세가 실제와 크게 벌어지면(장애물·외부 리셋) 실제로 되맞춘다 —
    장애물에 대고 계속 밀지 않고, 리셋 뒤 옛 자세로 안 끌려간다."""
    from app.services import web_leader as W
    integ = W.Integrator({f"joint{i+1}": 40.0 for i in range(1, 7)} | {"gripper": 30.0})
    # 실제 팔이 파킹으로 리셋됐다(밖에서). 명령 자세는 아직 40.
    integ.observe({"joint1": 0.0, "joint2": -100.0, "joint3": 100.0, "joint4": 0.0, "joint5": 0.0, "joint6": 0.0, "gripper": 0.0})
    assert integ.pose["joint2"] == -100.0 and integ.pose["joint1"] == 0.0, "리셋됐는데 명령이 옛 자세로 남았다"
    assert integ.pose["gripper"] == 30.0, "그리퍼는 사용자 명령을 유지해야 한다"
    # 작은 추종 지연이면 되맞추지 않는다 (조작이 튀지 않게)
    integ.observe({f"joint{i+1}": (-100.0 if i == 1 else 100.0 if i == 2 else 0.0) + 3 for i in range(6)} | {"gripper": 0.0})
    assert integ.pose["joint2"] == -100.0, "작은 지연에도 되맞추면 관절 모드가 튄다"
    # 루프가 양 모드에서 observe 를 부르는가
    src = (REPO / "backend" / "app" / "services" / "web_leader.py").read_text()
    loop = src.split("def _loop", 1)[1]
    assert "integ.observe(_follower_norm(reader))" in loop


def test_the_teleop_window_shows_a_loading_gate_and_skips_the_slow_scan_for_sim():
    """조종 창은 카메라 준비 전(스캔·연결) 화면을 덮어 오조작을 막고, 시뮬은 실기 카메라
    프로브(~5초)를 건너뛰고 알려진 sim 카메라를 바로 연결한다(사용자 보고 2026)."""
    src = (REPO / "frontend" / "src" / "pages" / "TeleopWindowPage.tsx").read_text()
    assert "const [loading, setLoading] = useState(true)" in src
    assert "조종 화면 준비 중" in src and "z-20" in src and "if (loading || moveBlock) return" in src, "로딩 오버레이가 클릭을 안 막는다"
    # 시뮬 빠른 경로: scan 대신 알려진 sim 카메라 직접 연결
    simpath = src.split("follower.startsWith('sim_')", 1)[1].split("} else {", 1)[0]
    assert "'sim:top', 'sim:front', 'sim:wrist'" in simpath
    assert "api.get" not in simpath, "시뮬 경로가 느린 스캔을 부른다"
    assert "api.get<Cam[] | { cameras: Cam[] }>('/cameras/scan')" in src, "실기 팔은 여전히 스캔한다"


def test_the_window_streams_only_the_big_camera_and_shows_video_vs_input_liveness():
    """⚠ **사용자 보고(2026)**: 중간중간 멈춤 — 영상인지 입력인지 구분 안 됨. 원인은 영구
    MJPEG 스트림 3개가 브라우저 HTTP/1.1 연결(6개) 중 3개를 물어 30Hz 입력 POST 가 굶은
    것(서버는 3스트림 다 풀 fps). 큰 화면만 스트림, 작은 둘은 스냅샷 폴링(연결 안 뭄).
    그리고 영상(스트림 fps)·입력(전송 중인가)을 따로 표시해 무엇이 멈췄는지 보인다."""
    src = (REPO / "frontend" / "src" / "pages" / "TeleopWindowPage.tsx").read_text()
    # 모든 카메라가 스냅샷 폴링 — 영구 MJPEG 스트림이 없다(연결 굶김·onLoad 오판 방지)
    assert "/stream" not in src, "아직 영구 스트림 img 가 남아 있다"
    big = src.split("relative bg-black rounded overflow-hidden\">", 1)[1].split("</div>", 1)[0]
    assert "/preview?t=${bigTick}" in big and "onLoad=" in big and "setBigTick" in big, "큰 화면이 자기속도 스냅샷 폴링이 아니다"
    # 영상/입력을 따로 보여 준다
    assert "영상 멈춤" in src and "영상 {videoFps" in src
    assert "입력 {inputLive" in src and "전달 중" in src
    assert "relay?.sent" in src, "입력 흐름을 relay 전송 증가로 판정하지 않는다"


def test_the_move_block_button_and_b_key_send_a_normalized_click():
    """블럭 옮기기 — 버튼·B 키로 켜고 큰 화면 클릭 → 정규화 u,v 를 /cube 로. 라우트는
    시뮬 카메라만 받는다. 클릭이 조종을 시작하지 않게 arena 전파를 막는다."""
    win = (REPO / "frontend" / "src" / "pages" / "TeleopWindowPage.tsx").read_text()
    assert "e.code === 'KeyB'" in win and "블럭 옮기기" in win
    assert "/leader/web/cube" in win and "placeBlock" in win
    assert "loading || moveBlock" in win, "블럭 모드에서 클릭이 조종을 시작하면 안 된다"
    assert "object-contain" in win and "naturalWidth" in win, "레터박스 보정 없이 픽셀→u,v 하면 어긋난다"
    router = (REPO / "backend" / "app" / "routers" / "web_leader.py").read_text()
    cube = router.split("async def move_cube", 1)[1].split("\n@router", 1)[0]
    assert 'cam.startswith("sim:")' in cube and '"cube_from_view"' in cube


def test_the_help_button_opens_a_structured_panel_matching_the_backend_mappings():
    """도움말 — 창 안 버튼(또는 Shift+/)이 div 오버레이를 띄운다. 한 줄 요약이 아니라
    관절·EE·공통을 키/설명 행으로 나눠 읽히게 한다(사용자 요청 2026). 매핑은 백엔드와
    어긋나면 안 된다 — ⚠ 옛 도움말은 관절5 를 T/G 로 적었지만 T 는 팔 리셋이고 관절5 는
    가운데 드래그·휠 선택으로만 움직인다."""
    win = (REPO / "frontend" / "src" / "pages" / "TeleopWindowPage.tsx").read_text()
    # 버튼(또는 Shift+/)이 오버레이를 여닫는다
    assert ">도움말</button>" in win and "e.code === 'Slash' && e.shiftKey" in win
    # 세 묶음을 키/설명 행으로
    assert "JOINT_ROWS" in win and "EE_ROWS" in win and "COMMON_ROWS" in win
    assert "조작 도움말" in win and win.count("<HelpSection ") == 3
    # 배경 클릭은 닫되 전파를 막아 조종(포인터 락)이 시작되지 않는다
    ov = win.split("{help && (", 1)[1].split(")}", 1)[0]
    assert "z-30" in ov and "e.stopPropagation(); setHelp(false)" in ov
    # ⚠ 관절5=T/G 오탐 재발 방지: 관절 묶음엔 T 행이 없고 관절5 는 가운데 드래그로
    joint = win.split("JOINT_ROWS", 1)[1].split("const EE_ROWS", 1)[0]
    assert "'T " not in joint and "T/G" not in joint, "관절5 를 다시 T 로 적었다(T 는 팔 리셋)"
    assert "'관절5'" in joint and "가운데 버튼 상하 드래그" in joint
