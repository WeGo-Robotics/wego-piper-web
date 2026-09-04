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
