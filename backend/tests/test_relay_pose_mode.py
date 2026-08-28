"""리더 텔레오퍼레이션 6D 자세 모드 (feature/teleoperation.md).

## 두 모드가 안전 면에서 다르다

    joint  리더 관절을 복제한다 → robotd `filter_goal` 을 **탄다**
    pose   리더 말단 6D 를 FK 로 구해 MoveP 로 준다 → 관절을 팔의 온보드 IK 가
           정하므로 `filter_goal` 이 걸 자리가 **없다**

그래서 pose 모드는 막는 것이 전부 `relay._send_pose` 에 있다. 하나라도 빠지면
그 모드는 **아무것도 안 막는다.** 여기 테스트가 그 목록을 지킨다.
"""

import math
from pathlib import Path


import numpy as np
import pytest

pytest.importorskip("piper_robot")
from piper_robot import kinematics as K  # noqa: E402
from piper_robot.armmodel import ArmModel  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
RELAY = REPO / "backend" / "app" / "services" / "relay.py"


# ── FK 가 팔과 같은 규약인가 ─────────────────────────────────────────────────

def test_the_pose_is_in_sdk_units():
    """`arm.read_end_pose()` 와 같은 형태여야 그대로 `EndPoseCtrl` 에 넣는다."""
    p = K.end_pose(np.zeros(6))
    assert set(p) == {"x", "y", "z", "rx", "ry", "rz"}
    assert all(isinstance(v, int) for v in p.values()), "SDK 는 정수를 받는다"


def test_the_rotation_convention_matches_the_arm():
    """⚠ 실측으로 정한 것이다. 팔로워에서 팔이 스스로 보고한 값과 대조했을 때
    `Rz·Ry·Rx` 가 **0.00°** 로 맞고 `Rx·Ry·Rz` 는 19.8° 어긋났다."""
    # Rz(30°) 하나만 준 회전은 yaw 30, roll·pitch 0 이어야 한다
    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
    rot = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    roll, pitch, yaw = K.rpy_from_matrix(rot)
    assert (round(roll, 6), round(pitch, 6), round(yaw, 4)) == (0.0, 0.0, 30.0)


def test_the_endpoint_is_the_flange_like_the_arm_reports():
    """팔이 보고하는 원점(link6 플랜지)과 같아야 한다 — 그리퍼 끝이면 13cm 틀린다."""
    q = np.zeros((1, 6))
    xyz = K.endpoint_xyz(q)[0] * 1000.0 * K.UM_PER_MM
    p = K.end_pose(q[0])
    assert abs(p["x"] - xyz[0]) < 2 and abs(p["z"] - xyz[2]) < 2


# ── 짐벌락 ──────────────────────────────────────────────────────────────────

def test_gimbal_lock_is_detected():
    """⚠ |pitch| 가 90° 에 가까우면 rx·rz 분해가 불안정하다. 거기서 나온 값을
    MoveP 로 보내면 **리더를 조금 움직였는데 팔이 홱 돈다.**"""
    assert K.GIMBAL_MARGIN_DEG > 0
    # pitch 90° 를 만드는 자세를 수치로 찾는다 — joint2·3 이 그 조합을 만든다
    found = False
    rng = np.random.default_rng(0)
    for _ in range(4000):
        q = rng.uniform(-2.0, 2.0, 6)
        tf = K.link_transforms(q[None, :])[0, K.geometry().index("link6")]
        pitch = abs(math.degrees(math.asin(max(-1, min(1, -tf[2, 0])))))
        if abs(pitch - 90.0) < 1.0:
            assert K.near_gimbal_lock(q), f"pitch {pitch:.1f}° 를 놓쳤다"
            found = True
            break
    if not found:
        pytest.skip("표본에서 짐벌락 자세가 안 나왔다")


def test_a_normal_pose_is_not_flagged():
    assert not K.near_gimbal_lock(np.zeros(6))


# ── pose 모드가 검사를 빠뜨리지 않는가 ──────────────────────────────────────

def test_every_guard_is_present():
    """IK 이전 문제들 — 관절 목표로 끝나므로 범위·변화율·데드맨은 robotd 것이다."""
    body = RELAY.read_text().split("def _send_pose", 1)[1].split("\n    def ", 1)[0]
    for guard, why in (
        ("near_gimbal_lock", "짐벌락"),
        ("lowest_z", "바닥"),
        ("POSE_MAX_STEP_MM", "한 걸음 상한"),
        ("sol.ok", "IK 해 없음"),
    ):
        assert guard in body, f"{why} 검사가 없다"


def test_a_blocked_frame_sends_nothing():
    """막혔으면 **보내지 않는다.** 자르거나 근사해서 보내면 사람이 의도하지
    않은 자세로 팔이 간다 — 관절 모드와 달리 되돌릴 필터가 없다."""
    body = RELAY.read_text().split("def _send_pose", 1)[1].split("\n    def ", 1)[0]
    # 모든 _block 뒤에는 return 이 따라야 한다
    for chunk in body.split("self._block(")[1:]:
        head = chunk.split("\n\n", 1)[0]
        assert "return" in head, f"막고 나서 계속 보낸다: {head[:80]}"


def test_pose_mode_goes_through_the_safety_filter():
    """⚠ **이게 재정의의 핵심이다.** 관절을 우리가 정하므로 관절 목표로 나가고,
    그러면 robotd 의 `filter_goal`(바닥·범위·변화율·데드맨)을 그대로 탄다.

    온보드 IK 로 MoveP 를 쏘던 때는 그게 통째로 빠졌다.
    """
    body = RELAY.read_text().split("def _send_pose", 1)[1].split("\n    def ", 1)[0]
    assert "self._writer.publish(goal)" in body, "관절 세그먼트로 안 나간다"
    assert "stream_end_pose" not in body, "아직 MoveP 로 쏜다"


def _unused_test_the_streaming_command_skips_the_enable_delay():
    """`move_end_pose` 는 매번 `EnablePiper()` + 200ms 를 쓴다 — 15Hz 스트림에
    그걸 쓰면 주기를 통째로 잡아먹어 5Hz 도 안 나온다."""
    arm = (REPO / "robot" / "piper_robot" / "arm.py").read_text()
    body = arm.split("def stream_end_pose", 1)[1].split("\n    def ", 1)[0]
    # ⚠ docstring 이 **왜** 그걸 안 쓰는지 설명하느라 그 이름을 적는다.
    #   `code_only` 는 `#` 주석만 걷으므로 docstring 을 따로 떼어낸다.
    #   이 저장소에서 네 번째다.
    code = body.split('"""', 2)[-1]
    assert "EnablePiper" not in code
    assert "EndPoseCtrl" in body and "ModeCtrl(0x01, 0x00" in body, "MOVE P 모드가 아니다"


def test_the_default_mode_is_the_filtered_one():
    """기본이 pose 면 사용자가 모르는 채로 필터 없는 모드를 쓰게 된다."""
    src = RELAY.read_text()
    assert 'mode: str = "joint"' in src
    router = (REPO / "backend" / "app" / "routers" / "robots.py").read_text()
    assert 'mode: str = "joint"' in router


def test_the_screen_says_the_filters_do_apply():
    """예전에는 그 반대를 적어야 했다 — 온보드 IK 로 MoveP 를 쏘던 때는
    안전 필터가 통째로 빠졌다. 재정의로 되돌아왔으므로 화면도 바뀌어야 한다."""
    panel = (REPO / "frontend" / "src" / "components" / "JogPanel.tsx").read_text()
    assert "똑같이 걸립니다" in panel
    assert "관절 구성이 다른 팔" in panel, "이 모드를 만든 이유가 안 적혀 있다"


# ── 고른 모드가 조용히 버려지면 안 된다 ──────────────────────────────────────

def test_an_unknown_field_is_refused():
    """⚠ **실제로 났던 고장이다.** 화면은 `mode: 'pose'` 를 보냈는데 그때 돌던
    게이트웨이에 그 필드가 없었다. Pydantic 기본값이 "모르는 필드는 무시" 라
    200 이 돌아오고 **관절 복제로 돌았다** — 화면은 6D 라고 표시한 채로.

    "6D 인데 왜 팔로워 관절이 따라 돌지" 로 보고됐다. 400 이면 바로 안다.
    """
    import pydantic

    from app.routers.robots import RelayStartRequest

    with pytest.raises(pydantic.ValidationError):
        RelayStartRequest(leader="a", follower="b", mdoe="pose")


def test_the_screen_shows_the_mode_the_server_reports():
    """고른 값을 보여주면 위 거짓말을 화면이 그대로 반복한다."""
    panel = (REPO / "frontend" / "src" / "components" / "JogPanel.tsx").read_text()
    assert "runningMode" in panel
    assert "runningMode !== relayMode" in panel, "불일치를 알리지 않는다"


def test_the_status_carries_the_mode():
    from app.services.relay import relay_session

    assert "mode" in relay_session.status()


# ── 손목 특이점 — 짐벌락과 **다른 조건** ────────────────────────────────────

def test_the_wrist_singularity_is_a_separate_guard():
    """⚠ 실측: `joint5 ≈ 0` 인 자세 3000개 중 짐벌락 가드가 잡은 것은 **0.2%**.

    짐벌락은 RPY **표현**의 문제(pitch ±90°), 손목 특이점은 **기구학**의
    문제(joint4·joint6 축이 겹침)다. 하나로 뭉치면 99.8% 를 놓친다.
    """
    q = np.array([0.3, 1.0, -0.8, 0.2, 0.0, 0.1])       # joint5 = 0
    assert K.near_wrist_singularity(q)
    assert not K.near_gimbal_lock(q), "이 자세는 짐벌락이 아니다 — 그래서 따로 필요하다"


def test_joint4_and_joint6_are_coaxial_at_the_singularity():
    """가드의 근거. joint5=0 에서 두 축 사이각이 0 이어야 한다."""
    g = K.geometry()
    q = np.zeros(6)
    tf = K.link_transforms(q[None, :])[0]
    a4 = tf[4, :3, :3] @ np.array(g.axis[4])
    a6 = tf[6, :3, :3] @ np.array(g.axis[6])
    assert abs(abs(float(a4 @ a6)) - 1.0) < 1e-9, "동축이 아니다"


def test_a_wrist_flip_costs_almost_nothing_at_the_singularity():
    """왜 걸음 상한이 이걸 못 잡는지. joint4 +20°/joint6 -20° 를 해도
    자세가 **거의 안 변하므로** 자세 기준 상한을 통과한다."""
    def pose(q):
        p = K.end_pose(np.array(q))
        return np.array([p["rx"], p["ry"], p["rz"]]) / 1000.0

    base = [0.3, 1.0, -0.8, 0.2, 0.0, 0.1]
    flip = [*base[:3], base[3] + math.radians(20), base[4], base[5] - math.radians(20)]
    moved = np.abs(((pose(flip) - pose(base) + 180) % 360) - 180).max()
    assert moved < 0.1, f"자세가 {moved:.2f}° 변했다 — 이 테스트의 전제가 틀렸다"


def test_the_threshold_leaves_a_real_margin():
    """10° 는 40°짜리 손목 뒤집기가 자세 3.4° 값이 되는 지점이다(실측).
    0 으로 두면 가드가 사실상 없다."""
    assert K.WRIST_SINGULAR_DEG >= 5.0


def test_the_wrist_guard_is_no_longer_needed_here():
    """⚠ 예전에는 `near_wrist_singularity` 로 막아야 했다. 팔의 **온보드 IK** 가
    joint4/joint6 분배를 자유롭게 골라 손목을 40° 뒤집었기 때문이다.

    이제는 IK 를 우리가 풀고 **직전 해를 시드로** 쓰므로 같은 가지에 머문다
    (위 `test_ik_stays_on_one_branch_through_the_singularity` 가 그걸 잰다).
    가드를 지운 것이 아니라 **원인이 사라졌다** — 함수는 남아 있다.
    """
    assert hasattr(K, "near_wrist_singularity"), "판정 자체는 남겨 둔다"
    body = RELAY.read_text().split("def _send_pose", 1)[1].split("\n    def ", 1)[0]
    assert "self._seed" in body, "시드로 이어지지 않으면 가드가 다시 필요하다"


# ── 팔이 못 간다고 할 때 ────────────────────────────────────────────────────

def test_the_arm_reports_its_own_failure():
    """⚠ 팔은 `GetArmStatus().arm_status` 로 IK 실패를 **직접 말한다**
    (0x02 无解, 0x03 奇异点). 우리는 같은 메시지에서 `ctrl_mode` 와 `err_code`
    만 읽고 이걸 안 봤다 — 그래서 "왜 안 가지" 를 추측했다."""
    from piper_robot.arm import Arm

    assert Arm.MOTION_STATUS[0x02] and Arm.MOTION_STATUS[0x03]
    assert 0x02 in Arm.MOTION_BAD and 0x03 in Arm.MOTION_BAD
    assert 0x00 not in Arm.MOTION_BAD, "정상을 실패로 세면 항상 막힌다"


def test_an_unsolvable_pose_says_which_arm_could_not():
    """⚠ 리더가 그 자세에 서 있다는 것은 **도달 가능하다는 증거이지 팔로워가
    갈 수 있다는 뜻이 아니다.** 팔이 다르면 작업공간부터 다르다."""
    body = RELAY.read_text().split("def _send_pose", 1)[1].split("\n    def ", 1)[0]
    assert "fm.name" in body, "어느 팔이 못 갔는지 말하지 않는다"
    assert "sol.reason" in body


def test_the_workspace_box_is_not_ik():
    """⚠ "작업 공간 밖" 은 IK 실패가 아니다 — 우리 상자다.

    실측: URDF 관절한계 안 무작위 자세 60,000개 중 상자 안은 **10.1%** 뿐이다.
    리더가 상자 밖 자세에 서 있는 것은 이상한 일이 아니라 당연한 일이다.
    """
    from piper_robot.endpose import WorkspaceBox

    box = WorkspaceBox()
    lim = np.array([(-2.6179938, 2.6179938), (0, 3.1415926), (-2.9670597, 0),
                    (-1.7453292, 1.7453292), (-1.2217304, 1.2217304), (-2.0943951, 2.0943951)])
    rng = np.random.default_rng(0)
    q = rng.uniform(lim[:, 0], lim[:, 1], size=(4000, 6))
    xyz = K.endpoint_xyz(q) * 1000.0
    inside = ((xyz[:, 0] >= box.x[0]) & (xyz[:, 0] <= box.x[1])
              & (xyz[:, 1] >= box.y[0]) & (xyz[:, 1] <= box.y[1])
              & (xyz[:, 2] >= box.z[0]) & (xyz[:, 2] <= box.z[1]))
    assert inside.mean() < 0.3, "상자가 넓어졌다 — 이 테스트의 근거를 다시 재라"


# ── 다른 팔을 붙일 수 있는가 (이 모드를 만든 이유) ──────────────────────────

def test_the_model_is_loaded_by_name():
    """SO-101 처럼 관절 구성이 다른 팔을 팔로워로 쓰려는 것이 이 모드의 이유다.
    Piper 관절값을 그 팔에 직접 대입할 수는 없다 — 6D 자세만 건너간다."""
    from piper_robot.armmodel import ArmModel

    m = ArmModel.load("piper")
    assert m.dof == 6
    assert "piper" in ArmModel.available()


def test_an_unregistered_arm_says_how_to_add_it():
    """없는 팔은 **어떻게 추가하는지** 말해야 한다 — 파일명만 던지면
    다음 사람이 그 명령을 다시 찾아야 한다."""
    from piper_robot.armmodel import ArmModel

    with pytest.raises(FileNotFoundError, match="build_arm_geometry"):
        ArmModel.load("no_such_arm")


def test_the_geometry_carries_joint_limits():
    """⚠ 한계를 같이 굽지 않으면 새 팔에서 IK 가 한계를 모르고
    **도달 불가능한 해**를 낸다."""
    with np.load(K.DATA) as z:
        assert "limits" in z
        assert z["limits"].shape[1] == 2


def test_the_ik_limit_margin_covers_the_measured_overshoot():
    """⚠ **실제 팔은 URDF 한계 밖에 앉아 있다** — 실측 joint3 +2.9°.
    여유 없이 자르면 리더가 지금 서 있는 자세를 IK 가 못 푼다(실제로 그랬다)."""
    assert K.IK_LIMIT_MARGIN_DEG >= 3.0


def test_the_jacobian_matches_finite_differences():
    """IK 가 이걸 딛고 선다. 틀리면 수렴이 **그럴듯하게** 어긋난다."""
    rng = np.random.default_rng(0)
    g = K.geometry()
    idx = g.index("link6")
    q = rng.uniform(K.joint_limits()[:, 0], K.joint_limits()[:, 1])
    j = K.jacobian(q)
    h = 1e-6
    for i in range(6):
        qp = q.copy()
        qp[i] += h
        a = K.link_transforms(q[None, :])[0, idx]
        b = K.link_transforms(qp[None, :])[0, idx]
        num = np.empty(6)
        num[:3] = (b[:3, 3] - a[:3, 3]) / h
        dr = (b[:3, :3] - a[:3, :3]) / h @ a[:3, :3].T
        num[3:] = [dr[2, 1], dr[0, 2], dr[1, 0]]
        assert np.abs(num - j[:, i]).max() < 1e-4


def test_ik_stays_on_one_branch_through_the_singularity():
    """⚠ **직전 해를 시드로 쓰는 이유.** 팔의 온보드 IK 는 여기서 손목을 40°
    뒤집었다(자세는 그대로인 채로). 우리 IK 는 이어져야 한다."""
    from piper_robot.armmodel import ArmModel

    m = ArmModel.load("piper")
    q = np.array([0.3, 1.0, -0.8, 0.2, -0.4, 0.1])
    seed = q.copy()
    worst = 0.0
    for j5 in np.linspace(-0.4, 0.4, 41):
        qt = q.copy()
        qt[4] = j5
        sol = m.ik(m.fk(qt), seed)
        if sol.ok:
            worst = max(worst, float(np.abs(np.degrees(sol.q - seed)).max()))
            seed = sol.q
    assert worst < 15.0, f"특이점에서 {worst:.0f}° 튀었다 — 가지가 바뀐다"


# ── SO-101 (관절 구성이 다른 팔) ─────────────────────────────────────────────

def _so101():
    from piper_robot.armmodel import ArmModel

    try:
        return ArmModel.load("so101")
    except FileNotFoundError:
        pytest.skip("so101 지오메트리가 없다")


def test_so101_is_five_dof_with_its_own_tip():
    """⚠ 말단 링크를 `link6` 로 하드코딩하면 Piper 밖에 못 쓴다.
    SO-101 의 말단은 `gripper_frame_link` 이고 메시가 없는 순수 좌표계다."""
    m = _so101()
    assert m.dof == 5
    assert m.geom.tip == "gripper_frame_link"


def test_the_chain_is_built_topologically_not_in_document_order():
    """⚠ SO-101 URDF 는 관절을 **말단부터** 적어 두었다. 문서 순서대로 읽으면
    사슬이 거꾸로 서고 FK 가 그럴듯하게 틀린다."""
    m = _so101()
    order = [m.geom.names[k] for k in range(len(m.geom.names)) if int(m.geom.qidx[k]) >= 0]
    assert order[0] == "shoulder_link" and order[-1] == "gripper_link"


def test_an_under_actuated_arm_gives_up_orientation_not_position():
    """⚠ 5축은 임의 6D 를 **원리적으로** 못 맞춘다 (무가중 실측 잔차 190mm/10°).
    무엇을 포기할지 정해야 하고, 텔레오퍼레이션에서는 위치가 먼저다."""
    m = _so101()
    assert m.weights[0] > m.weights[3], "자세보다 위치를 무겁게 줘야 한다"
    assert m.tol_deg > K.IK_TOL_DEG, "자세 허용오차를 안 풀면 늘 실패로 답한다"


def test_so101_ik_works_under_teleop_conditions():
    """연속 궤적에서 시드를 이어가는 것이 실제 조건이다 —
    실측: 200스텝 실패 0회, 위치 0.31mm, 한 스텝 최대 5.4°."""
    m = _so101()
    rng = np.random.default_rng(0)
    q = m.home().copy()
    seed = q.copy()
    fails = 0
    worst_step = 0.0
    for _ in range(120):
        q = np.clip(q + rng.normal(0, 0.03, m.dof), m.limits[:, 0], m.limits[:, 1])
        sol = m.ik(m.fk(q), seed)
        if not sol.ok:
            fails += 1
            continue
        worst_step = max(worst_step, float(np.abs(np.degrees(sol.q - seed)).max()))
        seed = sol.q
    assert fails == 0, f"연속 궤적에서 {fails}회 실패"
    assert worst_step < 20.0, f"한 스텝에 {worst_step:.0f}° 튀었다"


def test_the_transport_is_still_piper_only_and_says_so():
    """⚠ 기구학 모델은 팔 무관이지만 **명령 세그먼트는 아직 Piper 6축이다.**
    조용히 시작해서 루프 안에서 터지면 사용자는 '릴레이가 죽었다'만 본다."""
    from app.services.relay import _transport_mismatch

    assert not _transport_mismatch(ArmModel.load("piper"))
    why = _transport_mismatch(_so101())
    assert "5축" in why and "전송 계층" in why


def test_the_collision_origin_is_applied():
    """⚠ Piper 는 충돌 origin 이 전부 0 이라 그동안 무시해도 됐다. SO-101 은
    링크마다 다르다 — 무시하면 메시가 엉뚱한 자리에 놓여 **바닥 판정이 통째로
    틀린다.**"""
    builder = (REPO / "tools" / "build_arm_geometry.py").read_text()
    assert "_rot(pose[\"rpy\"])" in builder
    assert 'coll.find("origin")' in builder
