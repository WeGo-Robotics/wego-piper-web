"""장치가 사라진 것을 알아채는가 — CAN·카메라 (services/device_watch.py).

증상: USB 가 빠지거나 xHCI 컨트롤러가 죽으면 장치가 통째로 사라지는데
**화면은 그대로였다.** 스캔을 눌러도 목록이 안 바뀌고, 추론만 뒤늦게
"세그먼트가 없습니다"로 죽었다.

## 신호는 하나다 — **남아 있는데 멈춘 세그먼트**

없어진 세그먼트는 안 본다. 세그먼트가 없는 데는 이유가 둘이고(닫혀 있다 / 없어졌다)
그것만으로는 못 가른다 — 실제로 rsd 재시작 뒤 카메라를 안 연 정상 상태가
"사라졌습니다"로 떴다.

반면 **남아 있는데 멈춘 것**은 이유가 하나뿐이다: 발행자가 비정상으로 멈췄다.
닫으면 파일이 지워지고(`stop_publish`), 데몬은 기동할 때 남은 것을 치운다.
돌고 있던 카메라를 뽑는 것이 정확히 이 모양이다.
"""

import pytest

from app.services import device_watch as W


class _Arm:
    def __init__(self, iface, ready=True, role="follower"):
        self.iface, self.ready, self.role = iface, ready, role


class _Cam:
    def __init__(self, cam_id, ready=True, cam_type="realsense", label="", present=True):
        self.id, self.ready, self.cam_type, self.label = cam_id, ready, cam_type, label
        # 스캔이 이 장치를 봤는가. 멈춘 이유가 케이블인지 데몬인지를 이게 가른다.
        self.present = present
        self.name = cam_id


@pytest.fixture
def world(monkeypatch):
    """장치·데몬·세그먼트를 통째로 가짜로. 하드웨어 없이 모든 갈래를 만든다."""
    state = {"arms": [], "cams": [], "robotd": True, "rsd": True, "camerad": True,
             "arm_fresh": [], "arm_stale": [], "cam_fresh": [], "cam_stale": []}

    class _RM:
        arms = {}

    class _CM:
        cameras = {}

    rm, cm = _RM(), _CM()
    monkeypatch.setattr("app.services.robot_manager.robot_manager", rm)
    monkeypatch.setattr("app.services.robot_manager.robotd_available", lambda: state["robotd"])
    monkeypatch.setattr("app.services.realsense_manager.rs_available", lambda: state["rsd"])
    monkeypatch.setattr("app.services.v4l2_client.v4l2_hub.available", lambda: state["camerad"])
    monkeypatch.setattr("app.services.camera_manager.camera_manager", cm)
    # `(발행 중, 멈춘 채 남아 있는)` — 판정이 보는 유일한 입력이다
    monkeypatch.setattr(W, "_survey_arms",
                        lambda: (set(state["arm_fresh"]), set(state["arm_stale"])))
    monkeypatch.setattr(W, "_survey_cameras",
                        lambda: (set(state["cam_fresh"]), set(state["cam_stale"])))

    def _apply():
        rm.arms = {a.iface: a for a in state["arms"]}
        cm.cameras = {c.id: c for c in state["cams"]}

    state["apply"] = _apply
    return state


# ── 조용해야 할 때 ──────────────────────────────────────────────────────────

def test_quiet_when_everything_publishes(world):
    world["arms"] = [_Arm("can0")]
    world["arm_fresh"] = ["can0"]
    world["cams"] = [_Cam("rs:1:color")]
    world["cam_fresh"] = ["rs_1_color"]
    world["apply"]()
    assert W.DeviceWatch().check() == ([], [])


def test_idle_is_not_a_fault(world):
    """**회귀** — rsd 를 되살렸는데 카메라를 아직 안 열었을 뿐인 상태가
    "사라졌습니다"로 떴다. 세그먼트가 없는 것은 닫혀 있다는 뜻이기도 하다."""
    world["cams"] = [_Cam("rs:1:color")]
    world["arms"] = [_Arm("can0")]
    world["apply"]()                              # 발행 없음, 데몬은 살아 있음
    assert W.DeviceWatch().check() == ([], [])


# ── 쓰던 중에 빠졌다 ────────────────────────────────────────────────────────

def test_a_stopped_publisher_that_vanished_blames_the_cable(world):
    """스캔에도 안 보이면 뽑힌 것이다."""
    world["cams"] = [_Cam("rs:1:color", label="탑뷰"),
                     _Cam("rs:2:color", label="손목", present=False)]
    world["cam_fresh"] = ["rs_1_color"]
    world["cam_stale"] = ["rs_2_color"]
    world["apply"]()
    new, _ = W.DeviceWatch().check()
    assert len(new) == 1
    assert new[0].reason == "device_gone" and "손목" in new[0].text
    assert "USB" in new[0].text


def test_a_present_device_is_not_blamed_on_the_cable(world):
    """**회귀** — 노트북 **내장** 웹캠에 "USB 연결을 확인하세요" 가 떴다.

    뽑을 수도 없는 장치다. 발행이 멈춘 이유가 케이블이라고 단정하면 사용자를
    있는 장치를 뽑으러 보낸다. 스캔이 봤으면(`present`) 데몬이 스트림을 놓친 것이다.
    """
    world["cams"] = [_Cam("/dev/video0", cam_type="opencv", label="탑", present=True)]
    world["cam_stale"] = ["dev_video0"]
    world["apply"]()
    new, _ = W.DeviceWatch().check()
    assert [a.reason for a in new] == ["stalled"]
    assert "USB" not in new[0].text and "꽂혀 있습니다" in new[0].text


def test_one_arm_stopped_is_a_usb_problem(world):
    world["arms"] = [_Arm("can0", role="leader"), _Arm("can1", role="follower")]
    world["arm_fresh"] = ["can0"]
    world["arm_stale"] = ["can1"]
    world["apply"]()
    new, _ = W.DeviceWatch().check()
    assert [a.reason for a in new] == ["device_gone"]
    assert new[0].ident == "can1" and "USB" in new[0].text


def test_all_stopped_at_once_names_both_causes(world):
    """전부 한꺼번에 멈추는 건 개별 USB 문제가 아니다 — 데몬이거나 컨트롤러다.
    확인 방법이 다르므로 **둘 다** 적어준다."""
    world["arms"] = [_Arm("can0"), _Arm("can1")]
    world["arm_stale"] = ["can0", "can1"]
    world["apply"]()
    new, _ = W.DeviceWatch().check()
    assert [a.reason for a in new] == ["all_gone"]
    assert "systemctl" in new[0].text and "컨트롤러" in new[0].text


def test_a_single_device_is_not_called_all(world):
    """하나뿐이면 "전부"와 "그 하나"를 구분할 수 없다 — 케이블을 보라는 게 맞다."""
    world["arms"] = [_Arm("can1")]
    world["arm_stale"] = ["can1"]
    world["apply"]()
    new, _ = W.DeviceWatch().check()
    assert [a.reason for a in new] == ["device_gone"]


# ── 데몬이 죽은 것과 구분한다 ───────────────────────────────────────────────

def test_dead_daemon_does_not_blame_usb(world):
    """**회귀** — robotd 를 멈췄더니 먼저 "USB 를 확인하세요"가 떴다.
    데몬 문제인데 사용자를 케이블 뽑으러 보내면 안 된다."""
    world["arms"] = [_Arm("can0"), _Arm("can1")]
    world["arm_stale"] = ["can0", "can1"]
    world["robotd"] = False
    world["apply"]()
    new, _ = W.DeviceWatch().check()
    assert [a.reason for a in new] == ["daemon_down"]
    assert "USB" not in new[0].text


def test_dead_daemon_is_reported_even_with_no_segments(world):
    """세그먼트가 하나도 없어도, 쓰겠다고 등록해둔 장치가 있으면 알린다."""
    world["cams"] = [_Cam("rs:1:color", ready=True)]
    world["rsd"] = False
    world["apply"]()
    new, _ = W.DeviceWatch().check()
    assert [a.reason for a in new] == ["daemon_down"]


def test_unregistered_devices_do_not_trigger_daemon_alerts(world):
    """등록도 안 한 장치 때문에 경보가 뜨면 소음이 된다."""
    world["cams"] = [_Cam("rs:1:color", ready=False)]
    world["rsd"] = False
    world["apply"]()
    assert W.DeviceWatch().check() == ([], [])


def test_one_camera_daemon_down_does_not_condemn_the_other(world):
    """rsd 가 죽어도 웹캠은 멀쩡할 수 있다."""
    world["cams"] = [_Cam("rs:1:color"), _Cam("/dev/video2", cam_type="opencv")]
    world["cam_fresh"] = ["dev_video2"]
    world["rsd"] = False
    world["apply"]()
    new, _ = W.DeviceWatch().check()
    assert [a.reason for a in new] == ["daemon_down"]
    assert "rsd" in new[0].name


# ── 전이에서만 ──────────────────────────────────────────────────────────────

def test_repeats_are_not_re_announced(world):
    """2초마다 같은 말을 하면 아무도 안 읽는다."""
    world["arms"] = [_Arm("can0"), _Arm("can1")]
    world["arm_fresh"] = ["can0"]
    world["arm_stale"] = ["can1"]
    world["apply"]()
    w = W.DeviceWatch()
    assert len(w.check()[0]) == 1
    assert w.check() == ([], [])
    assert len(w.alerts()) == 1                  # 현재 목록에는 남는다


def test_recovery_is_announced_and_clears(world):
    world["arms"] = [_Arm("can0"), _Arm("can1")]
    world["arm_fresh"] = ["can0"]
    world["arm_stale"] = ["can1"]
    world["apply"]()
    w = W.DeviceWatch()
    w.check()
    world["arm_fresh"], world["arm_stale"] = ["can0", "can1"], []
    new, gone = w.check()
    assert new == [] and len(gone) == 1 and gone[0].ident == "can1"
    assert w.alerts() == []


# ── 진짜 shm 으로 ───────────────────────────────────────────────────────────

def test_a_stale_segment_is_seen_as_stale():
    """**회귀** — USB 를 뽑아도 세그먼트는 남는다.

        ok, frame = self._cap.read()
        if ok and frame is not None:
            self._publish(frame)        # ← 실패하면 발행만 안 할 뿐

    `stop_publish()` 는 명시적 `disconnect()` 에서만 불리므로 파일은 그대로다.
    그래서 존재가 아니라 **마지막 발행 시각**으로 판정한다.
    """
    import numpy as np
    from piper_shm import Publisher

    name = "pytest_stale_cam"
    pub = Publisher(name, height=4, width=4, channels=3)
    try:
        pub.publish(np.zeros((4, 4, 3), dtype=np.uint8))
        fresh, stale = W._survey_cameras()
        assert name in fresh and name not in stale

        old, W.STALE_S = W.STALE_S, -1.0         # 시간이 흐른 것으로 친다
        try:
            fresh, stale = W._survey_cameras()
            assert name in stale, "파일이 남아 있으면 살아 있다고 본다 — 그게 원래 버그다"
        finally:
            W.STALE_S = old
    finally:
        pub.close()
        try:
            from piper_shm import unlink
            unlink(name)
        except Exception:
            pass


def test_message_text_comes_from_the_backend():
    """화면이 문장을 조립하면 한쪽만 고쳐져 어긋난다 (`usb_warning` 과 같은 규칙)."""
    from pathlib import Path

    assert "USB 연결을 확인하세요" in Path(W.__file__).read_text()
    page = (Path(W.__file__).resolve().parents[3]
            / "frontend/src/components/DeviceAlerts.tsx").read_text()
    assert "USB" not in page.split("*/", 1)[-1], "화면이 문구를 직접 만들고 있다"


# ── 데몬이 판정한 것을 먼저 쓴다 ────────────────────────────────────────────

def test_the_daemon_verdict_wins_over_inference(world, monkeypatch):
    """**회귀** — "카메라를 뽑으면 바로 알람이 안 온다".

    게이트웨이가 세그먼트 나이로 추론하면 STALE_S(5초) + 감시 주기(2초)가 걸리고,
    읽기가 계속 성공하는 장치에서는 아예 못 잡는다. 장치를 쥔 데몬은 노드가
    사라진 것을 1초 안에 보므로, 그 판정을 **먼저** 쓴다.
    """
    world["cams"] = [_Cam("/dev/video2", cam_type="opencv", label="탑")]
    world["cam_fresh"] = ["dev_video2"]           # 세그먼트는 아직 신선하다
    world["apply"]()
    monkeypatch.setattr("app.services.v4l2_client.v4l2_hub.lost",
                        lambda: [{"id": "/dev/video2", "at": 1.0}])
    monkeypatch.setattr("app.services.realsense_manager.realsense_hub.lost", lambda: [])
    new, _ = W.DeviceWatch().check()
    assert [a.reason for a in new] == ["device_gone"]
    assert "탑" in new[0].text


def test_daemon_loss_is_declared_on_node_removal():
    """camerad 가 `/dev/videoN` 사라짐을 **결정적 증거**로 삼는가.

    읽기 실패는 일시적일 수 있지만 노드가 없어진 것은 아니다 — USB 를 뽑으면
    커널이 즉시 지운다.
    """
    import ast
    from pathlib import Path

    src = (Path(W.__file__).resolve().parents[3] / "cam/piper_cam/hub.py").read_text()
    loop = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef) and n.name == "_loop")
    calls = {n.func.attr for n in ast.walk(loop)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "exists" in calls, "노드 존재를 안 본다 — 읽기 실패만으로는 늦다"
    assert "_declare_lost" in calls, "결론을 안 내고 계속 돈다"
