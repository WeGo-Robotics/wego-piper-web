"""바닥 필터 — 기구학 기반 (refactor/robotd-safety.md).

**전부 하드웨어 없이 돈다.** 문서가 필터를 순수 함수로 못박은 이유가 이것이다 —
안전 로직을 실제 팔로 검증하는 건 그 자체가 위험하다.
"""

import numpy as np
import pytest

pytest.importorskip("piper_robot")
from piper_robot import JOINT_ORDER, Reason, SafetyConfig, filter_goal  # noqa: E402
from piper_robot import kinematics as K  # noqa: E402
from piper_robot.safety import FloorConfig, floor_guard  # noqa: E402

HOME = dict.fromkeys(JOINT_ORDER, 0.0)


def pose(v) -> dict:
    """정규화 6벡터 → 관절 dict (그리퍼 0)."""
    return dict(zip(JOINT_ORDER, list(v) + [0.0]))


def low(v) -> float:
    """그 자세에서 팔의 최저점 (m)."""
    return float(K.lowest_z(K.norm_to_rad(np.array([list(v)], dtype=float)))[0])


# 바닥을 뚫는 자세와 그 위 자세 — 실측으로 고른 값이다
DEEP = [0.0, 90.0, -90.0, 0.0, 60.0, 0.0]      # 최저점 약 -6cm
HIGH = [0.0, 20.0, -40.0, 0.0, 0.0, 0.0]       # 팔을 세운 자세


# ── 기구학 ───────────────────────────────────────────────────────────────────

def test_the_geometry_ships_with_the_package():
    """런타임에 URDF 서브모듈이 없어도 돌아야 한다 — robotd 는 호스트 배포다."""
    assert K.available()
    g = K.geometry()
    assert len(g.names) >= 7
    assert len(g.pts) > 1000


def test_the_gripper_is_part_of_the_arm():
    """⚠ 바닥에 **먼저 닿는 것이 그리퍼**다. link6 플랜지만 보면 13cm 를 놓친다."""
    g = K.geometry()
    assert any(n.startswith("gripper") for n in g.names)
    fingers = [i for i, n in enumerate(g.names) if n.startswith("gripper_link")]
    assert fingers, "손가락 링크가 지오메트리에 없다"
    assert all((g.pt_link == i).any() for i in fingers), "손가락에 점이 없다"


def test_the_base_cannot_violate_the_floor():
    """뿌리 링크는 팔이 볼트로 고정된 바로 그 면이다 — 검사 대상이 아니다.

    넣으면 **영자세부터 걸린다**: base_link 메시가 z=0 에서 시작하는데 덮개가
    그걸 반지름만큼 아래로 읽는다.
    """
    g = K.geometry()
    root = int(np.flatnonzero(g.parent < 0)[0])
    assert not g.movable[g.pt_link == root].any()
    assert low([0.0] * 6) > 0, "영자세가 바닥 아래로 읽힌다"


def test_the_covering_reads_low_never_high():
    """덮개는 **항상 실제보다 아래**를 봐야 한다 — 틀리는 방향이 안전한 쪽이다.

    각 점은 복셀 중심이고 반지름이 `cell·√3/2` 다. 그 반지름을 빼므로 어떤
    자세에서도 실제 메시보다 낮게 나온다.
    """
    g = K.geometry()
    assert g.radius > 0
    # 반지름을 안 뺀 값(= 복셀 중심들의 최저)은 항상 뺀 값보다 위에 있다
    q = K.norm_to_rad(np.array([DEEP, HIGH, [0.0] * 6]))
    tf = K.link_transforms(q)
    link, pts = g.pt_link[g.movable], g.pts[g.movable]
    centres = (np.einsum("tnj,nj->tn", tf[:, link, 2, :3], pts)
               + tf[:, link, 2, 3]).min(axis=1)
    assert np.allclose(centres - g.radius, K.lowest_z(q))


# ── 필터 동작 ────────────────────────────────────────────────────────────────

def test_a_command_into_the_floor_is_cut_short_not_refused():
    """⚠ 거부하면 정책이 계속 바닥을 향하는 동안 **팔이 굳는다.**
    마지막 안전 지점까지 가면 불연속이 없고 자연히 감속한다 (문서)."""
    cfg = FloorConfig(min_z=-0.02)
    near = [0.0, 77.3, -77.3, 0.0, 51.6, 0.0]     # 한계 바로 위
    assert low(near) > cfg.min_z
    assert low(DEEP) < cfg.min_z

    applied, changed = floor_guard(pose(near), pose(DEEP), cfg)
    assert changed
    got = [applied[j] for j in K.ARM_JOINTS]
    assert got != DEEP, "목표를 그대로 통과시켰다"
    assert low(got) >= cfg.min_z, "적용값이 여전히 한계 아래다"


def test_the_path_is_checked_not_just_the_endpoint():
    """⚠ 정책은 33ms 간격의 웨이포인트를 준다. 그 사이는 팔이 보간하므로
    **양끝이 안전해도 중간이 바닥을 뚫을 수 있다.**"""
    cfg = FloorConfig(min_z=-0.02, sweep_steps=8)
    rng = np.random.default_rng(3)
    lo = np.array([-100.0, 0, -100, -100, -100, -100])
    hi = np.array([100.0, 100, 0, 100, 100, 100])
    for _ in range(6000):
        a, b = rng.uniform(lo, hi), rng.uniform(lo, hi)
        if low(a) < cfg.min_z or low(b) < cfg.min_z:
            continue
        mid = K.lowest_z(K.norm_to_rad(a + np.linspace(0, 1, 9)[:, None] * (b - a)))
        if mid.min() >= cfg.min_z:
            continue
        # 양끝은 안전한데 중간이 뚫는다 — 끝점만 보는 필터는 여기서 통과시킨다
        _, changed = floor_guard(pose(list(a)), pose(list(b)), cfg)
        assert changed, "중간이 바닥을 뚫는 경로를 통과시켰다"
        return
    pytest.skip("표본에서 그런 경로가 안 나왔다")


def test_an_arm_already_below_the_floor_can_still_come_up():
    """⚠ 이걸 막으면 **복구가 불가능해진다** — 팔이 바닥에 박힌 채 굳는다."""
    cfg = FloorConfig(min_z=-0.02)
    assert low(DEEP) < cfg.min_z
    _, changed = floor_guard(pose(DEEP), pose(HIGH), cfg)
    assert not changed, "위로 올라가는 명령을 막았다"


def test_an_arm_already_below_the_floor_cannot_go_deeper():
    cfg = FloorConfig(min_z=-0.02)
    deeper = [v * 1.05 for v in DEEP]
    assert low(deeper) < low(DEEP)
    applied, changed = floor_guard(pose(DEEP), pose(deeper), cfg)
    assert changed
    assert [applied[j] for j in K.ARM_JOINTS] == DEEP, "현재 자세를 유지해야 한다"


def test_a_safe_command_is_untouched():
    cfg = FloorConfig(min_z=-0.02)
    applied, changed = floor_guard(pose(HIGH), pose(HIGH), cfg)
    assert not changed
    assert applied == pose(HIGH)


def test_disabled_means_disabled():
    """녹화 중에는 꺼야 한다 — 켠 채 녹화하면 **실행되지 않은 동작이 데이터셋에
    들어간다** (LeRobot 이 `_sent_action` 을 버리고 필터 이전 값을 기록한다)."""
    _, changed = floor_guard(pose(HIGH), pose(DEEP), FloorConfig(enabled=False))
    assert not changed


def test_the_guard_is_pure():
    """하드웨어 없이 부를 수 있고 입력을 안 건드린다."""
    cfg = FloorConfig(min_z=-0.02)
    now, goal = pose(HIGH), pose(DEEP)
    before = (dict(now), dict(goal))
    floor_guard(now, goal, cfg)
    floor_guard(now, goal, cfg)
    assert (now, goal) == before


# ── 다른 필터와의 관계 ───────────────────────────────────────────────────────

def test_the_floor_reason_reaches_the_caller():
    """robotd 가 이 사유를 로그·텔레메트리로 낸다 — 발동률을 봐야 하기 때문이다."""
    cfg = SafetyConfig(floor=FloorConfig(min_z=-0.02), max_step=200.0)
    _, reason = filter_goal(pose(HIGH), pose(DEEP), cfg)
    assert reason is Reason.FLOOR


def test_the_deadman_still_outranks_the_floor():
    """소비자가 죽으면 바닥 판정 이전에 **그 자리에 선다.**"""
    cfg = SafetyConfig(floor=FloorConfig(min_z=-0.02))
    applied, reason = filter_goal(pose(HIGH), pose(DEEP), cfg, deadman_tripped=True)
    assert reason is Reason.DEADMAN
    assert applied == pose(HIGH)


def test_the_floor_runs_after_the_rate_clamp():
    """⚠ 실제로 나갈 값을 검사해야 한다. 변화율로 잘리기 전 값을 검사하면
    **있지도 않을 경로를 막는다.**"""
    cfg = SafetyConfig(floor=FloorConfig(min_z=-0.02), max_step=1.0)
    # 한 스텝 1.0 이면 HIGH 에서 DEEP 쪽으로 아주 조금밖에 못 간다 → 여전히 안전
    applied, reason = filter_goal(pose(HIGH), pose(DEEP), cfg)
    assert reason is Reason.CLAMPED_RATE
    assert low([applied[j] for j in K.ARM_JOINTS]) >= cfg.floor.min_z


def test_nan_is_caught_before_the_kinematics_sees_it():
    """FK 에 NaN 이 들어가면 최저점이 NaN 이 되고 비교가 전부 False 가 된다 —
    조용히 통과한다. 유한성 검사가 먼저 걸러야 한다."""
    cfg = SafetyConfig(floor=FloorConfig(min_z=-0.02))
    _, reason = filter_goal(pose(HIGH), pose([float("nan")] + HIGH[1:]), cfg)
    assert reason is Reason.NOT_FINITE
