"""하드웨어 영점 — 모터 플래시 (piper SDK `JointConfig(set_zero=0xAE)`, CAN 0x475).

## ⚠ 소프트웨어 영점과 섞이면 안 된다

    소프트웨어  `joints.JOINT_CALIBRATION`, `~/.piper/parking/*.json`
                우리 파일 안의 표. 고쳐도 팔은 모르고 언제든 되돌린다.
    하드웨어    여기. 모터 드라이버 **플래시**에 굽는다. 전원을 꺼도 남고
                **되돌리는 명령이 SDK 에 없다.** raw 값의 의미가 바뀐다.

여기 테스트는 **CAN 을 건드리지 않는다.** 실제로 구우면 되돌릴 수 없으므로,
경로·가드·문구만 본다.
"""

import ast
from pathlib import Path

import pytest

from conftest import code_only

REPO = Path(__file__).resolve().parents[2]
ARM = REPO / "robot" / "piper_robot" / "arm.py"
ROUTER = REPO / "backend" / "app" / "routers" / "robots.py"
MODAL = REPO / "frontend" / "src" / "components" / "ZeroCalibrationModal.tsx"


# ── SDK 호출이 맞는가 ────────────────────────────────────────────────────────

def test_it_uses_the_hardware_zero_command():
    """`JointConfig(set_zero=0xAE)` 다 — 소프트웨어 표를 고치는 게 아니다."""
    src = ARM.read_text()
    body = src.split("def _flash_zero_once", 1)[1].split("\n    def ", 1)[0]
    assert "JointConfig(joint_num=motor, set_zero=0xAE)" in body
    assert "GripperCtrl(0, 1000, 0x01, 0xAE)" in body, "그리퍼는 별도 명령이다"


def test_the_gripper_is_motor_seven():
    """SDK 규약: 1~6 이 관절, 7 이 그리퍼."""
    from piper_robot.arm import Arm

    assert Arm.ZERO_MOTOR["gripper"] == 7
    assert [Arm.ZERO_MOTOR[f"joint{i}"] for i in range(1, 7)] == [1, 2, 3, 4, 5, 6]


def test_the_response_is_cleared_before_sending():
    """⚠ 안 지우면 **이전 명령의 성공 응답**을 읽고 이번 것도 성공이라고 보고한다."""
    body = ARM.read_text().split("def _flash_zero_once", 1)[1]
    clear = body.index("ClearRespSetInstruction")
    send = body.index("set_zero=0xAE")
    assert clear < send, "응답을 지우기 전에 명령을 보낸다"


def test_no_response_is_not_success():
    """`is_set_zero_successfully` 가 -1 이면 **응답이 없는 것**이다.
    보냈으니 됐다고 치면 CAN 이 반쯤 죽었을 때 조용히 실패한다."""
    body = ARM.read_text().split("def _flash_zero_once", 1)[1].split("\n    def ", 1)[0]
    tree = ast.parse(ARM.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_flash_zero_once")
    # 성공(True)을 돌려주는 return 은 flag == 1 가지 하나여야 한다
    # ⚠ `"ok"` 키와 **짝지어진** 값을 본다. 예전에는 "값 중에 True 가 있나" 로
    #   봤는데, `{"ok": False, "fatal": True}` 처럼 다른 True 가 생기자 실패를
    #   성공으로 셌다 — 검사가 조용히 헐거워지는 모양이다.
    def _ok_value(d: ast.Dict):
        for k, v in zip(d.keys, d.values):
            if isinstance(k, ast.Constant) and k.value == "ok":
                return v.value if isinstance(v, ast.Constant) else None
        return None

    oks = [n for n in ast.walk(fn) if isinstance(n, ast.Return)
           and isinstance(n.value, ast.Dict) and _ok_value(n.value) is True]
    assert len(oks) == 1, "성공을 돌려주는 자리가 하나가 아니다"
    assert "flag == 1" in body


# ── 가드 ────────────────────────────────────────────────────────────────────

def test_a_moving_arm_cannot_be_zeroed():
    """⚠ 움직이는 중에 구우면 **엉뚱한 자세가 영점이 된다.** 되돌릴 수 없으므로
    나중에 알아차려도 늦다."""
    body = ROUTER.read_text().split('@router.post("/zero")', 1)[1].split("@router.", 1)[0]
    for act in ("INFERENCE", "RECORDING", "TELEOP"):
        assert act in body, f"{act} 실행 중에도 영점을 굽는다"


def test_only_robotd_touches_can():
    """게이트웨이가 SDK 를 직접 부르면 CAN 을 두 프로세스가 연다."""
    router = code_only(ROUTER.read_text())
    assert "JointConfig" not in router
    assert "set_hardware_zero" in router


def test_the_daemon_exposes_it():
    src = (REPO / "daemons" / "robotd.py").read_text()
    assert '"set_hardware_zero"' in src and '"read_raw_all"' in src


# ── 화면이 되돌릴 수 없음을 말하는가 ────────────────────────────────────────

def test_the_modal_says_it_cannot_be_undone():
    """버튼만 있으면 되돌릴 수 없는 조작을 실수로 누른다."""
    src = MODAL.read_text()
    assert "되돌릴 수 없습니다" in src
    assert "플래시" in src


def test_the_modal_names_what_it_invalidates():
    """영점을 옮기면 raw 의 의미가 바뀌고 그 위의 것이 전부 어긋난다."""
    src = MODAL.read_text()
    for what in ("바닥", "파킹", "데이터셋"):
        assert what in src, f"{what} 이 어긋난다는 말이 없다"


def test_the_modal_shows_raw_not_normalised():
    """⚠ 정규화는 우리 표를 거친 값이라 영점과 **함께 흔들린다.**
    무엇을 굽는지 보려면 팔이 직접 말하는 숫자여야 한다."""
    src = MODAL.read_text()
    assert "/robots/joints/raw/" in src
    assert "parking/joints" not in src, "정규화 값을 보여주고 있다"


def test_there_is_no_zero_everything_button():
    """되돌릴 수 없는 조작 일곱 개를 묶어 실행할 이유가 없다 —
    하나가 틀리면 어느 것이 틀렸는지 알 수 없다."""
    src = code_only(MODAL.read_text())
    assert "전체" not in src and "Set All" not in src


def test_confirmation_is_required():
    src = MODAL.read_text()
    assert "confirm(" in src and "danger: true" in src


def test_it_is_not_called_parking_calibration():
    """[파킹 보정]은 소프트웨어다. 이름이 겹치면 사용자가 그걸 누른다."""
    page = (REPO / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    assert "영점(HW)" in page
    assert "파킹 보정" in page, "소프트웨어 쪽 버튼이 사라졌다"
