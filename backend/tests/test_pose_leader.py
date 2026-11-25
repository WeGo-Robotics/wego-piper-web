"""자세 리더 — 손에 든 6자유도 자세로 팔로워를 끈다 (feature/pika.md §5).

지키는 것: 정합 순간 점프 0, 손의 움직임이 정합 순간 손 좌표계로 재어져 팔 끝 좌표계로 간다(축 정렬),
추적을 잃으면 그 자리에 붙잡는다, 소스가 튀면 해제한다, 빠른 손은 직선으로 따라잡는다, 못 가는 곳·바닥은
붙잡는다, 그리퍼는 mm → 그 팔의 mm, 두 번 쥐기는 정합 토글, 릴레이가 끝나면 같이 끝난다.

IK 는 진짜 PIPER 모델이다 — 축 정렬은 모델의 끝단 축과 맞아야 뜻이 있다.
"""

from __future__ import annotations

import asyncio
import math
import os
import time

import numpy as np
import pytest

from app.services import pose_leader as P
from app.services.one_euro import OneEuro

WORK = [0, -37, 64, 0, 30, 0]          # 작업 자세(정규화) — 끝단이 앞·아래 40° 를 본다
SRC = (1.0, 0.5, 1.2)                  # 트래커 좌표계 어딘가


@pytest.fixture(scope="module")
def model():
    from piper_robot.armmodel import ArmModel
    return ArmModel.load("piper")


def _q(norm):
    from piper_robot import kinematics as K
    return K.norm_to_rad(np.array([norm], float))[0]


def _rec(pos=SRC, quat=(0, 0, 0, 1), op=40.0, ok=True, enc=True, trig=0):
    return {"pos": pos, "quat": quat, "opening_mm": op, "tracking_ok": ok, "encoder_ok": enc, "trigger_seq": trig}


class Clock:
    def __init__(self):
        self.t = 100.0

    def tick(self, n=1):
        self.t += n / P.HZ
        return self.t


def _engaged(model, **kw):
    m = P.PoseMapper(model, _q(WORK), 50.0, **kw)
    c = Clock()
    m.step(_rec(), 0.01, c.tick())
    m.engage(m.last_src)
    return m, c


def _run(m, c, recs, age=0.01):
    for r in recs:
        m.step(r, age, c.tick())
    return m


def _settle(m, c, rec, n=30):
    return _run(m, c, [rec] * n)


def _tip(model, m):
    return model.fk(m.out_q)


# ── 필터 (PR #18 원본의 자체 검사) ──

def test_one_euro_is_the_pr18_original():
    rng = np.random.default_rng(0)
    t = np.arange(0, 4, 1 / 30)
    f = OneEuro(1.0, 0.5)
    still = np.array([f(0.01 * rng.standard_normal(2), ti) for ti in t])
    assert np.std(still[30:]) < 0.4 * 0.01
    f = OneEuro(1.0, 0.5)
    ramp = np.array([f(np.array([1.0 * ti, 0.0]), ti) for ti in t])
    lag = (t[-1] - ramp[-1, 0]) / 1.0
    assert 0 < lag < 0.1
    assert (np.diff(ramp[:, 0]) > 0).all() and ramp[-1, 0] <= t[-1]
    f = OneEuro(1.0, 0.5)
    assert np.allclose(f([3.0], 0.0), 3.0) and np.allclose(f([5.0], 0.0), 3.0)


# ── 정합·축 정렬 ──

def test_engaging_does_not_move_the_arm(model):
    m = P.PoseMapper(model, _q(WORK), 50.0)
    c = Clock()
    before = m.step(_rec(), 0.01, c.tick())
    m.engage(m.last_src)
    after = _settle(m, c, _rec(), 10).goal()
    assert max(abs(after[k] - before[k]) for k in before if k != "gripper") < 1e-6


def test_pushing_the_handle_forward_moves_the_tip_along_its_approach_axis(model):
    """손잡이 x(앞) → PIPER 끝단 z(앞). 정합 순간 손 좌표계로 재므로 트래커의 방위는 상관없다."""
    m, c = _engaged(model)
    T0 = _tip(model, m)
    _run(m, c, [_rec((SRC[0] + 0.05 * min(i, 10) / 10, SRC[1], SRC[2])) for i in range(1, 40)])
    d = (_tip(model, m)[:3, 3] - T0[:3, 3]) * 1000
    assert np.linalg.norm(d) == pytest.approx(50.0, abs=1.0)
    assert np.dot(d / np.linalg.norm(d), T0[:3, 2]) > 0.999, "끝단 z(앞) 방향이어야 한다"


def test_the_motion_is_measured_in_the_hand_frame_at_registration(model):
    """손잡이를 트래커 z 축으로 90° 돌려 쥐고 정합해도, 손잡이 '앞' 으로 밀면 팔 끝은 앞으로 간다."""
    m = P.PoseMapper(model, _q(WORK), 50.0)
    c = Clock()
    yaw90 = (0, 0, math.sin(math.pi / 4), math.cos(math.pi / 4))     # 손잡이 x 가 트래커 +y 를 본다
    m.step(_rec(quat=yaw90), 0.01, c.tick())
    m.engage(m.last_src)
    T0 = _tip(model, m)
    _run(m, c, [_rec((SRC[0], SRC[1] + 0.04 * min(i, 10) / 10, SRC[2]), yaw90) for i in range(1, 40)])
    d = _tip(model, m)[:3, 3] - T0[:3, 3]
    assert np.dot(d / np.linalg.norm(d), T0[:3, 2]) > 0.999


def test_tilting_the_handle_nose_down_tilts_the_tip_down(model):
    m, c = _engaged(model)
    T0 = _tip(model, m)
    a = math.radians(20)
    _settle(m, c, _rec(quat=(0, math.sin(a / 2), 0, math.cos(a / 2))), 40)
    T1 = _tip(model, m)
    assert P.step_size(T0, T1)[1] == pytest.approx(20.0, abs=0.3)
    assert T1[2, 2] < T0[2, 2], "앞 축이 더 아래를 본다"
    assert m.blocked == ""


def test_position_scale_shrinks_the_hand_motion(model):
    m, c = _engaged(model, scale=0.5)
    T0 = _tip(model, m)
    _run(m, c, [_rec((SRC[0] + 0.06 * min(i, 10) / 10, SRC[1], SRC[2])) for i in range(1, 40)])
    assert np.linalg.norm(_tip(model, m)[:3, 3] - T0[:3, 3]) * 1000 == pytest.approx(30.0, abs=1.0)


def test_changing_the_scale_while_engaged_re_registers_without_a_jump(model):
    m, c = _engaged(model)
    _run(m, c, [_rec((SRC[0] + 0.05 * min(i, 10) / 10, SRC[1], SRC[2])) for i in range(1, 40)])
    before = m.goal()
    m.set_params(scale=0.5)
    after = _settle(m, c, _rec((SRC[0] + 0.05, SRC[1], SRC[2])), 10).goal()
    assert max(abs(after[k] - before[k]) for k in before) < 0.2


def test_released_the_arm_holds_and_the_hand_moves_freely(model):
    m, c = _engaged(model)
    _run(m, c, [_rec((SRC[0] + 0.05 * min(i, 10) / 10, SRC[1], SRC[2])) for i in range(1, 15)])
    m.disengage()
    held = m.goal()
    _run(m, c, [_rec((SRC[0] - 0.2, SRC[1] + 0.1, SRC[2]))] * 20)
    assert m.goal() == held, "해제하면 지금 내는 값에 멈춘다 — 필터가 남은 걸 끌고 가지 않는다"
    # 다시 정합 — 손이 딴 데 있어도 점프 0, 거기서 이어 쓴다
    m.engage(m.last_src)
    again = _settle(m, c, _rec((SRC[0] - 0.2, SRC[1] + 0.1, SRC[2])), 5).goal()
    assert max(abs(v - held[k]) for k, v in again.items()) < 1e-6


# ── 붙잡기·해제 ──

def test_lost_tracking_holds_the_arm_where_it_is(model):
    m, c = _engaged(model)
    _run(m, c, [_rec((SRC[0] + 0.03, SRC[1], SRC[2]))] * 30)
    held = m.goal()
    for r, age in ((_rec(ok=False), 0.01), (_rec((SRC[0] + 0.08, SRC[1], SRC[2])), 0.9), (None, None)):
        m.step(r, age, c.tick())
        assert m.lost and m.blocked.startswith("추적 상실")
        assert max(abs(m.goal()[k] - held[k]) for k in held) < 0.05
    assert m.engaged, "가림은 흔하다 — 해제하지 않고 붙잡는다"


def test_a_source_jump_releases_instead_of_crawling_there(model):
    m, c = _engaged(model)
    _settle(m, c, _rec(), 5)
    grip = m.goal()["gripper"]
    m.step(_rec((SRC[0] + 0.25, SRC[1], SRC[2]), op=5.0), 0.01, c.tick())      # 1/30초에 25cm
    assert not m.engaged and "튀어" in m.note
    assert m.goal()["gripper"] == grip, "해제한 그 틱에 그리퍼만 따라가면 안 된다"


def test_tracking_back_somewhere_else_releases(model):
    m, c = _engaged(model)
    _settle(m, c, _rec(), 5)
    for _ in range(20):
        m.step(_rec(ok=False), 0.01, c.tick())
    m.step(_rec((SRC[0], SRC[1] + 0.2, SRC[2])), 0.01, c.tick())       # 가려진 사이 손이 20cm 옮겨졌다
    assert not m.engaged


def test_a_fast_hand_is_followed_in_straight_capped_steps(model):
    m, c = _engaged(model)
    T0 = _tip(model, m)
    target = (SRC[0] + 0.08, SRC[1], SRC[2])                          # 한 틱 80mm — 빠르지만 손이다
    m.step(_rec(target), 0.01, c.tick())
    assert m.catching_up
    assert np.linalg.norm(m.T[:3, 3] - T0[:3, 3]) * 1000 == pytest.approx(P.STEP_MM, abs=0.5)
    _settle(m, c, _rec(target), 40)
    assert not m.catching_up and m.engaged
    assert np.linalg.norm(_tip(model, m)[:3, 3] - T0[:3, 3]) * 1000 == pytest.approx(80.0, abs=1.0)


def test_toward_slerps_rotation_and_caps_both():
    a = np.eye(4)
    b = P.pose_matrix((0.1, 0, 0), (0, 0, math.sin(math.radians(30)), math.cos(math.radians(30))))   # 60° 요
    out, cut = P.toward(a, b, 30.0, 20.0)
    mm, deg = P.step_size(a, out)
    assert cut and mm == pytest.approx(30.0) and deg == pytest.approx(20.0, abs=1e-6)
    assert P.step_size(out, b)[1] == pytest.approx(40.0, abs=1e-6), "같은 축 위에서 잘린다"


def test_out_of_reach_holds_and_comes_back(model):
    m, c = _engaged(model)
    T0 = _tip(model, m)
    path = [_rec((SRC[0] + 0.02 * i, SRC[1], SRC[2])) for i in range(1, 60)]      # 1.2m 앞으로 — 팔이 못 간다
    _run(m, c, path)
    assert m.blocked.startswith("도달 불가") and m.engaged
    reach = np.linalg.norm(_tip(model, m)[:3, 3] - T0[:3, 3])
    _run(m, c, [_rec((SRC[0] + 0.02 * i, SRC[1], SRC[2])) for i in range(59, 0, -1)])
    _settle(m, c, _rec(), 30)
    assert m.blocked == "" and reach > 0.1
    assert np.linalg.norm(_tip(model, m)[:3, 3] - T0[:3, 3]) < 0.002, "손이 돌아오면 팔도 돌아온다"


def test_the_floor_holds_the_arm_but_lets_it_climb_back(model):
    """작업 자세에선 어깨 쪽 고정 점(7.6cm)이 가장 낮다 — 손을 내려 그리퍼가 그보다 내려가야 바닥이 뜻이 있다."""
    down = [_rec((SRC[0], SRC[1], SRC[2] - 0.004 * i)) for i in range(1, 40)]   # 손잡이 위(z) = 끝단 −x
    m, c = _engaged(model, floor_cm=5.0)
    _run(m, c, down)
    assert m.blocked.startswith("바닥 한계")
    assert model.lowest_z(m.q) * 100 >= 5.0
    # 바닥 설정을 나중에 켜서 이미 한계 아래에 있어도 — 올라오는 길은 열려 있고, 더 내려가는 길만 막힌다
    m, c = _engaged(model)
    _run(m, c, down[:30])
    low = model.lowest_z(m.q) * 100
    assert low < 6.0 and m.blocked == ""
    m.floor_cm = 7.0
    _run(m, c, [_rec((SRC[0], SRC[1], SRC[2] - 0.12 - 0.004 * i)) for i in range(1, 4)])
    assert m.blocked.startswith("바닥 한계")
    _run(m, c, [_rec((SRC[0], SRC[1], SRC[2] - 0.12 + 0.004 * i)) for i in range(1, 6)])
    assert m.blocked == "" and model.lowest_z(m.q) * 100 > low


# ── 그리퍼 ──

def test_the_gripper_follows_the_opening_in_mm_of_that_arm(model):
    m, c = _engaged(model, grip_max_um=98000)       # 대형 그리퍼
    _settle(m, c, _rec(op=49.0), 1)
    assert m.goal()["gripper"] == pytest.approx(50.0)
    _settle(m, c, _rec(op=120.0), 1)
    assert m.goal()["gripper"] == 100.0
    _settle(m, c, _rec(op=None), 1)
    assert m.goal()["gripper"] == 100.0, "벌림이 없으면 그대로"
    _settle(m, c, _rec(op=10.0, enc=False), 1)
    assert m.goal()["gripper"] == 100.0, "엔코더가 죽었으면 그대로"
    m.disengage()
    _settle(m, c, _rec(op=0.0), 1)
    assert m.goal()["gripper"] == 100.0, "해제 중엔 그리퍼도 붙잡는다"


# ── 정합 도우미 ──

def test_align_compares_the_tilt_of_the_tip_and_the_handle(model):
    m = P.PoseMapper(model, _q(WORK), 50.0)
    m.step(_rec(), 0.01, 100.0)
    a = m.align()
    assert a["arm"]["pitch"] == pytest.approx(-40.6, abs=1.0) and a["arm"]["roll"] == pytest.approx(0, abs=0.5)
    assert a["hand"] == {"pitch": 0.0, "roll": 0.0}
    down = math.radians(40)
    m.step(_rec(quat=(0, math.sin(down / 2), 0, math.cos(down / 2))), 0.01, 100.1)
    assert m.align()["hand"]["pitch"] == pytest.approx(-40.0, abs=0.1)


# ── 세션 — 진짜 shm, 가짜 릴레이 ──

class FakeRelays:
    def __init__(self):
        self.live: dict[str, str] = {}        # leader → follower
        self.started = []

    def start(self, leader, follower, mode, la, fa):
        self.started.append((leader, follower, mode, la, fa))
        self.live[leader] = follower

    def by_leader(self, leader):
        f = self.live.get(leader)
        return None if f is None else type("S", (), {"follower": f, "leader": leader})()

    def stop(self, follower=None):
        self.live = {k: v for k, v in self.live.items() if follower is not None and v != follower}

    def status(self, follower=None):
        lead = next((k for k, v in self.live.items() if v == follower), None)
        return {"leader": lead, "holding": lead is not None, "sent": 7, "stale": False, "blocked": ""}


@pytest.fixture
def rig(monkeypatch):
    from piper_shm import arm as shm_arm
    from piper_shm.pose import PoseWriter

    from app.services import relay

    tag = f"t{os.getpid()}"
    src_id, follower = f"test_src_{tag}", f"test_follower_{tag}"
    fw = shm_arm.StateWriter(follower)
    fw.publish({f"joint{i + 1}": float(v) for i, v in enumerate(WORK)} | {"gripper": 20.0})
    pw = PoseWriter(src_id)
    pw.publish(SRC, (0, 0, 0, 1), opening_mm=30.0, encoder_ok=True, trigger_seq=5)
    fake = FakeRelays()
    monkeypatch.setattr(relay, "relays", fake)
    monkeypatch.setattr("app.services.robot_manager._call", lambda *a, **k: {})
    monkeypatch.setattr(P, "extension_for", lambda source: None)
    pool = P.PoseLeaderPool()
    yield type("Rig", (), {"pool": pool, "pw": pw, "src": src_id, "follower": follower, "relays": fake})()
    pool.stop()
    pw.close()
    fw.close()


def _leader(rig):
    from piper_shm import arm as shm_arm
    r = shm_arm.StateReader(P.leader_name(rig.follower))
    try:
        return r.read()["values"]
    finally:
        r.close()


def _wait(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_a_session_starts_released_at_the_follower_pose_and_relays_its_virtual_leader(rig):
    st = rig.pool.start(rig.src, rig.follower)
    assert st["leader"] == f"pose_{rig.follower}"
    assert rig.relays.started == [(f"pose_{rig.follower}", rig.follower, "joint", "piper", "piper")]
    v = _leader(rig)
    assert v["joint2"] == pytest.approx(-37.0, abs=0.01) and v["gripper"] == pytest.approx(20.0)
    assert _wait(lambda: rig.pool.status()["sessions"][0].get("engaged") is False)


def test_a_double_squeeze_toggles_registration_and_the_arm_follows(rig):
    rig.pool.start(rig.src, rig.follower)
    s = rig.pool.get(rig.follower)
    assert _wait(lambda: s.trigger_seq == 5), "시작 전의 횟수는 기준일 뿐 — 토글이 아니다"
    assert not s.mapper.engaged
    rig.pw.publish(SRC, (0, 0, 0, 1), opening_mm=30.0, encoder_ok=True, trigger_seq=6)
    assert _wait(lambda: s.mapper.engaged)
    j2 = _leader(rig)["joint2"]
    for i in range(1, 21):
        rig.pw.publish((SRC[0] + 0.05 * i / 20, SRC[1], SRC[2]), (0, 0, 0, 1), opening_mm=30.0, encoder_ok=True, trigger_seq=6)
        time.sleep(0.02)
    assert _wait(lambda: abs(_leader(rig)["joint2"] - j2) > 5), "손을 앞으로 밀면 가상 리더가 움직인다"
    assert _leader(rig)["gripper"] == pytest.approx(30000 / 68000 * 100, abs=0.1)
    rig.pw.publish((SRC[0] + 0.05, SRC[1], SRC[2]), (0, 0, 0, 1), opening_mm=30.0, encoder_ok=True, trigger_seq=7)
    assert _wait(lambda: not s.mapper.engaged)
    # 소스가 다시 떠 0 부터 세면 새 기준 — 토글하지 않는다
    rig.pw.publish((SRC[0] + 0.05, SRC[1], SRC[2]), (0, 0, 0, 1), opening_mm=30.0, encoder_ok=True, trigger_seq=0)
    assert _wait(lambda: s.trigger_seq == 0) and not s.mapper.engaged


def test_one_source_does_not_drive_two_arms_and_one_arm_takes_one_source(rig):
    rig.pool.start(rig.src, rig.follower)
    with pytest.raises(RuntimeError, match="이미"):
        rig.pool.start(rig.src, rig.follower)
    with pytest.raises(RuntimeError, match="두 팔"):
        rig.pool.start(rig.src, "other_arm")


def test_starting_without_the_source_says_so(rig):
    with pytest.raises(RuntimeError, match="자세 소스"):
        rig.pool.start("no_such_source", rig.follower)
    with pytest.raises(ValueError):
        rig.pool.start("../etc", rig.follower)


def test_when_the_relay_ends_the_session_ends_and_removes_its_leader(rig):
    from piper_shm import arm as shm_arm

    rig.pool.start(rig.src, rig.follower)
    rig.relays.stop(rig.follower)                 # E-stop · 다른 화면이 팔로워를 놓았다
    assert _wait(lambda: not rig.pool.sessions())
    assert f"pose_{rig.follower}.state" not in shm_arm.list_segments()
    assert "릴레이" in rig.pool.status()["ended"][0]["ended"]


def test_params_are_range_checked_and_reach_running_sessions(rig):
    rig.pool.start(rig.src, rig.follower)
    with pytest.raises(ValueError):
        rig.pool.set_params(min_cutoff=0.0)
    rig.pool.set_params(min_cutoff=1.0, beta=0.5)
    m = rig.pool.get(rig.follower).mapper
    assert m._filter.min_cutoff == 1.0 and m._filter.beta == 0.5


def test_estop_stops_pose_sessions_too(monkeypatch):
    from app.services import exclusivity as ex
    from app.services import jog, relay, teleop

    seen = []
    monkeypatch.setattr(P.pose_leaders, "stop", lambda *a: seen.append("pose") or [])
    monkeypatch.setattr(relay.relays, "stop", lambda *a: seen.append("relay") or [])
    monkeypatch.setattr(jog.jog_session, "stop", lambda *a: seen.append("jog"))

    async def _kill():
        seen.append("session")
    monkeypatch.setattr(teleop.teleop_session, "kill", _kill)
    asyncio.run(ex._stop_teleop())
    assert "pose" in seen and "relay" in seen


# ── 확장에 조명·진동 ──

def test_feedback_buzzes_when_blocked_and_again_while_it_stays_blocked(monkeypatch):
    sent = []
    monkeypatch.setattr(P._Feedback, "_run", lambda self: None)
    fb = P._Feedback("pika", "pika_sense_l")
    monkeypatch.setattr(fb, "_send", lambda: sent.append(dict(fb._state)) or fb._state.update(buzz=False))
    clock = [1000.0]
    monkeypatch.setattr(P.time, "monotonic", lambda: clock[0])
    fb.set(True, False)
    assert fb._state == {"engaged": True, "blocked": False, "buzz": False}
    fb.set(True, True)
    assert fb._state["buzz"], "막힌 순간 한 번"
    fb._send()
    clock[0] += 1.0
    fb.set(True, True)
    assert not fb._state["buzz"], "1초 뒤엔 아직"
    clock[0] += 1.5
    fb.set(True, True)
    assert fb._state["buzz"], "계속 막혀 있으면 2초마다"
