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
    """⚠ 이 모드에는 `filter_goal` 이 없다. 여기 목록이 **유일한** 방어선이다."""
    body = RELAY.read_text().split("def _send_pose", 1)[1].split("\n    def ", 1)[0]
    for guard, why in (
        ("near_gimbal_lock", "짐벌락"),
        ("lowest_z", "바닥"),
        ("WorkspaceBox", "작업 공간"),
        ("POSE_MAX_STEP_MM", "한 걸음 상한"),
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


def test_pose_mode_opens_no_action_segment():
    """⚠ 열어 놓고 관절 목표를 안 쓰면 robotd 데드맨이 '현재 자세 유지' 를
    JointCtrl 로 내려보내 **우리 MoveP 와 힘겨루기**를 한다."""
    src = RELAY.read_text()
    assert 'if mode == "joint":' in src
    assert "_POSE_MODE" in src


def test_the_streaming_command_skips_the_enable_delay():
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


def test_the_screen_says_the_filters_do_not_apply():
    panel = (REPO / "frontend" / "src" / "components" / "JogPanel.tsx").read_text()
    assert "온보드 IK" in panel
    assert "걸리지 않습니다" in panel
