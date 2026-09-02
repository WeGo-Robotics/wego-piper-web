"""관절 슬립 방어 — 리셋을 슬립 센서로 (piper_sdk #120).

과부하로 로터↔출력축이 미끄러지면 피드백은 명령값을 따라가며 **조용히
거짓말한다** — fault 도 없어 소프트웨어는 평소에 볼 수 없다. 0x150(0x02)
리셋만이 보고 프레임을 실제에 재동기화하고, 그 순간의 간극이 곧 쌓인 슬립이다.

그래서: 수집·추론 시작에 리셋을 넣고(수집이 최다 트리거 환경), 리셋 전후
피드백 차이를 재서 사람에게 말하고, 영점 굽기는 리셋 뒤에만 허락한다 —
슬립을 영점으로 오진해 굽는 것이 이 조작의 최악 실패다(2026-09-02 실사례).
"""

import inspect
from pathlib import Path

from app.services import robot_manager as rm

_ROOT = Path(__file__).resolve().parents[2]


def test_slip_warning_names_the_joints_and_degrees():
    report = [{"iface": "can1", "slip_raw": [0, 2500, 0, 0, -85000, 120]}]
    ws = rm.slip_warnings(report)
    assert len(ws) == 1
    assert "joint2 +2.5°" in ws[0] and "joint5 -85.0°" in ws[0], ws[0]
    assert "joint6" not in ws[0], "임계 밑 관절까지 나열하면 경고가 소음이 된다"
    assert "can1" in ws[0] and "#120" in ws[0]


def test_noise_below_threshold_stays_quiet():
    """정지 상태 잡음(수십 raw)마다 경고가 뜨면 아무도 안 읽는다."""
    assert rm.slip_warnings([{"iface": "can1", "slip_raw": [50, -120, 0, 0, 0, 0]}]) == []
    # 옛 robotd(slip_raw 없음)와도 조용히 호환된다
    assert rm.slip_warnings([{"iface": "can1", "error": None, "cleared": True}]) == []


def test_slip_is_measured_on_feedback_only():
    """⚠ `read_joints_raw` 는 지령(0x155~7)을 우선한다 — 슬립 계측은 '피드백이
    거짓말하다 리셋에 재동기화되는' 간극을 재는 것이라 지령을 섞으면 무너진다."""
    from piper_robot.arm import Arm

    src = inspect.getsource(Arm._feedback_joints_locked)
    assert "GetArmJointMsgs" in src
    assert "GetArmJointCtrl" not in src, "지령 폴백이 섞였다 — 계측이 무너진다"

    clear = inspect.getsource(Arm.clear_error)
    assert "MotionCtrl_1(0x02, 0, 0)" in clear, "0x150 리셋이 사라졌다"
    assert "slip_raw" in clear, "리셋이 슬립을 재지 않는다"


def test_recording_start_resets_the_followers():
    """수집이 슬립의 최다 트리거 환경인데 시작 리셋이 없었다 — 방어 공백.
    리더는 안 건드린다: 마스터 모드가 흔들릴 수 있다(#35 계열)."""
    src = (_ROOT / "backend" / "app" / "routers" / "recording.py").read_text()
    assert "clear_arm_errors" in src and "slip_warnings" in src
    body = src.split("prepare_arms(arm_ports", 1)[1]
    assert "robot_ports if bimanual else [body.robot_port]" in body, \
        "follower 만 리셋해야 한다 — teleop_ports 가 섞이면 리더 마스터 모드가 위험하다"


def test_zero_flashing_requires_a_reset_first():
    """⚠ 오진 방지 — 슬립으로 밀린 보고값을 '영점이 틀어졌다'로 읽고 구우면
    실제 영자세가 아닌 곳이 영점이 된다(되돌리는 명령 없음). 리셋이 크게
    재동기화되면 굽지 않고 멈추고, 다시 누르면 그때는 간극이 없어 통과한다."""
    src = (_ROOT / "backend" / "app" / "routers" / "robots.py").read_text()
    body = src.split('@router.post("/zero")', 1)[1].split("@router.", 1)[0]
    reset_at = body.index("clear_arm_errors")
    zero_at = body.index("set_hardware_zero(body.iface")
    assert reset_at < zero_at, "리셋이 굽기보다 먼저여야 한다"
    assert "slip_warnings" in body and "409" in body, "슬립이 드러나면 굽기를 멈춰야 한다"


def test_both_start_responses_carry_the_warnings_and_the_pages_show_them():
    """문구는 백엔드가 만들고(응답 arm_reset.warnings) 화면은 띄우기만 한다."""
    for f in ("recording.py", "models.py"):
        src = (_ROOT / "backend" / "app" / "routers" / f).read_text()
        assert '"arm_reset"' in src, f"{f} 응답에 슬립 경고가 없다"
    for f in ("RecordingPage.tsx", "InferencePage.tsx"):
        src = (_ROOT / "frontend" / "src" / "pages" / f).read_text()
        assert "arm_reset?.warnings" in src, f"{f} 가 슬립 경고를 안 띄운다"


def test_the_zero_modal_can_reset_and_refresh_by_hand():
    """영점 창에서 리셋(0x150)과 위치 새로고침을 직접 누를 수 있다 — 절차의
    1번이 리셋이다. 리셋 라우트는 움직이는 중이면 거절하고 슬립 간극을 돌려준다."""
    src = (_ROOT / "backend" / "app" / "routers" / "robots.py").read_text()
    body = src.split('@router.post("/reset")', 1)[1].split("@router.", 1)[0]
    assert "clear_arm_errors" in body and "slip_warnings" in body
    assert "movers" in body, "움직이는 중 리셋을 안 막는다 — 급정지 해제가 상태를 흔든다"

    modal = (_ROOT / "frontend" / "src" / "components" / "ZeroCalibrationModal.tsx").read_text()
    assert "/robots/reset" in modal, "모달에 리셋 버튼이 없다"
    assert "위치 새로고침" in modal
    assert "간극 없음" in modal, "간극이 없을 때도 결과를 말해야 한다 — 침묵은 실패처럼 보인다"
