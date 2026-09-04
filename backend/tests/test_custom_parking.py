"""커스텀 파킹 — **저장한 자세로 실제로 가는가.**

실기에서 이렇게 났다: 사람이 몇 번을 저장했는데 "원점으로" 는 늘 기본 자세로
갔다. 저장은 게이트웨이(`~/.config/piper-web/parking/`)에, 이동은 robotd 가
자기 폴더(`~/.piper/parking/`)에서 읽고 있었다. **UI 는 "저장됨" 이라 말하고
팔은 다른 데로 갔다** — 실패가 조용해서 저장하는 쪽을 의심하게 만든다.

경계는 `hub.py` 가 정해 뒀다: 사람이 고른 프리셋은 게이트웨이가 갖고 robotd 는
인자로 받는다. 같은 사실을 두 프로세스에 두면 갈라진다.
"""

import asyncio
import inspect

from app.routers import robots as R


class _Arm:
    connected = True

    def __init__(self):
        self.got = "안 불림"

    def go_parking(self, target=None):
        self.got = target
        return True


def _go(iface: str, arm, saved, monkeypatch):
    monkeypatch.setattr(R.robot_manager, "arms", {iface: arm}, raising=False)
    monkeypatch.setattr(R, "_load_custom_parking", lambda i: saved)
    body = R.ConnectRequest(iface=iface)
    return asyncio.run(R.parking_go(body))


def test_the_saved_pose_reaches_the_arm(monkeypatch):
    """⚠ 저장한 자세가 이동까지 **실려 가야** 한다. 안 실리면 팔은 기본 자세로
    가면서 UI 는 성공이라 말한다 — 사용자가 저장을 다시 하게 만든다."""
    saved = {"joint1": 0.0, "joint2": -102.36, "joint3": 100.53,
             "joint4": 0.64, "joint5": 0.0, "joint6": 0.4, "gripper": 33.24}
    arm = _Arm()
    _go("can3", arm, saved, monkeypatch)
    assert arm.got == saved, f"저장한 자세가 안 실렸다: {arm.got}"


def test_no_saved_pose_falls_back(monkeypatch):
    """저장이 없으면 `None` 이 가고 robotd 가 기본 자세를 쓴다."""
    arm = _Arm()
    _go("can3", arm, None, monkeypatch)
    assert arm.got is None


def test_robotd_does_not_keep_its_own_parking_store():
    """⚠ robotd 가 파킹을 **직접 읽으면** 저장 위치가 다시 갈라진다.
    `go_parking` 은 인자로만 받아야 한다."""
    from piper_robot import arm as arm_mod

    for name in ("PARKING_DIR", "_load_custom_parking", "_save_custom_parking"):
        assert not hasattr(arm_mod, name), (
            f"robot/piper_robot/arm.py 에 {name} 이 다시 생겼다 — "
            "커스텀 파킹은 게이트웨이가 갖고 인자로 넘긴다"
        )
    sig = inspect.signature(arm_mod.Arm.go_parking)
    assert "target" in sig.parameters, "go_parking 이 자세를 인자로 안 받는다"


# ── 파킹 보정 창 ─────────────────────────────────────────────────────────────

def _page() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[2]
            / "frontend/src/pages/RobotsPage.tsx").read_text()


def test_the_three_buttons_are_independent():
    """⚠ **순서를 강제하면 되돌아갈 수 없다.** 예전에는 `ready → moving →
    adjusting → saving` 한 줄로 흘러서, 다시 파킹으로 보내려면 창을 닫았다
    열어야 했다. 보정은 "맞을 때까지 되풀이하는" 일이라 그 흐름과 안 맞는다."""
    from conftest import code_only

    src = code_only(_page())
    modal = src.split("function ParkingCalibrationModal", 1)[1].split("\nfunction ", 1)[0]
    for label in ("파킹", "현재 위치 읽기", "저장"):
        assert label in modal, f"[{label}] 버튼이 없다"
    # 단계 기계가 남아 있으면 다시 순서가 생긴다
    assert "'adjusting'" not in modal and '"adjusting"' not in modal, \
        "단계 상태가 남아 있다 — 순서가 다시 강제된다"


def test_saving_uses_what_was_captured_not_the_polled_state():
    """⚠ 예전 `handleSave` 는 `readJoints()` 를 부른 **직후** `joints` 를
    저장했는데, React 상태는 그 자리에서 안 바뀐다 — **직전 폴링 값**(최대
    300ms 전 자세)이 저장됐다. 찍어 둔 값을 저장해야 무엇이 들어가는지 안다."""
    from conftest import code_only

    src = code_only(_page())
    modal = src.split("function ParkingCalibrationModal", 1)[1].split("\nfunction ", 1)[0]
    save = modal.split("const save = async", 1)[1][:400]
    assert "positions: captured" in save, "찍어 둔 값이 아니라 폴링 값을 저장한다"
    assert "readJoints()" not in save, "저장하면서 읽으면 또 한 박자 늦는다"


def test_parking_waits_for_the_arm_to_settle_not_a_fixed_delay():
    """⚠ 고정 3초는 자세가 멀면 못 닿고 가까우면 괜히 기다린다 — 둘 다 사람이
    "왜 엉뚱한 데서 멈췄지" 로 만난다. 영점 굽기 준비도 같은 이유로 시간이
    아니라 상태를 본다 (`_prepare_for_config_locked`)."""
    from conftest import code_only

    src = code_only(_page())
    modal = src.split("function ParkingCalibrationModal", 1)[1].split("\nfunction ", 1)[0]
    park = modal.split("const goParking = async", 1)[1][:900]
    assert "setTimeout(async" not in park, "고정 지연으로 도착을 가정한다"
    assert "still" in park, "멈췄는지 보지 않는다"
