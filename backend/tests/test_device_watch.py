"""장치가 사라진 것을 알아채는가 — CAN·카메라 (services/device_watch.py).

증상: USB 가 빠지거나 xHCI 컨트롤러가 죽으면 장치가 통째로 사라지는데
**화면은 그대로였다.** 스캔을 눌러도 목록이 안 바뀌고, 추론만 뒤늦게
"세그먼트가 없습니다"로 죽었다.

여기서 잠그는 것:

1. 쥐고 있던 장치의 발행이 끊기면 알린다
2. **데몬이 죽은 것과 USB 가 빠진 것을 구분한다** — 안 가르면 데몬 문제인데
   USB 를 확인하러 가게 만든다
3. **전이에서만** 알린다 — 2초마다 반복하면 아무도 안 읽는다
4. 안 쥐고 있는 장치는 알리지 않는다 (경보가 소음이 되면 진짜를 놓친다)
"""

import pytest

from app.services import device_watch as W


class _Arm:
    def __init__(self, iface, connected=True, ready=True, role="follower"):
        self.iface, self.connected, self.ready, self.role = iface, connected, ready, role


class _Cam:
    def __init__(self, cam_id, connected=True, cam_type="realsense", label=""):
        self.id, self.connected, self.cam_type, self.label = cam_id, connected, cam_type, label
        self.name = cam_id


@pytest.fixture
def world(monkeypatch):
    """장치·데몬·세그먼트를 통째로 가짜로. 하드웨어 없이 모든 갈래를 만든다."""
    state = {"arms": [], "cams": [], "robotd": True, "rsd": True, "camerad": True,
             "arm_segs": [], "cam_segs": []}

    class _RM:
        arms = {}

    rm = _RM()
    monkeypatch.setattr("app.services.robot_manager.robot_manager", rm)
    monkeypatch.setattr("app.services.robot_manager.robotd_available",
                        lambda: state["robotd"])
    monkeypatch.setattr("app.services.realsense_manager.rs_available",
                        lambda: state["rsd"])
    monkeypatch.setattr("app.services.v4l2_client.v4l2_hub.available",
                        lambda: state["camerad"])

    class _CM:
        cameras = {}

    cm = _CM()
    monkeypatch.setattr("app.services.camera_manager.camera_manager", cm)
    monkeypatch.setattr("piper_shm.arm.list_segments",
                        lambda: [f"{i}.state" for i in state["arm_segs"]])
    monkeypatch.setattr("piper_shm.list_segments", lambda: list(state["cam_segs"]))

    def _apply():
        rm.arms = {a.iface: a for a in state["arms"]}
        cm.cameras = {c.id: c for c in state["cams"]}

    state["apply"] = _apply
    return state


def _watch(world=None, arms=(), cams=()):
    """감시자 하나. **먼저 발행을 보여준다** — 본 적 없는 장치는 잃었다고 하지 않는다.

    실제 순서와 같다: 장치가 돌고 있는 동안 감시가 돌다가, 어느 순간 발행이 끊긴다.
    """
    w = W.DeviceWatch()
    if world is not None:
        before_arms, before_cams = world["arm_segs"], world["cam_segs"]
        world["arm_segs"], world["cam_segs"] = list(arms), list(cams)
        world["apply"]()
        w.check()                                # 이 상태를 "정상"으로 기억시킨다
        world["arm_segs"], world["cam_segs"] = before_arms, before_cams
        world["apply"]()
    return w


# ── 아무 일도 없을 때 ──

def test_quiet_when_everything_publishes(world):
    world["arms"] = [_Arm("can0"), _Arm("can1")]
    world["arm_segs"] = ["can0", "can1"]
    world["cams"] = [_Cam("rs:1:color")]
    world["cam_segs"] = ["rs_1_color"]
    world["apply"]()
    new, gone = _watch(world, ["can0", "can1"], ["rs_1_color"]).check()
    assert new == [] and gone == []


def test_unheld_devices_never_alert(world):
    """등록도 연결도 안 한 장치가 안 보이는 건 정상이다.

    이걸 알리면 스캔만 해도 경보가 뜨고, 경보가 소음이 되면 진짜를 놓친다.
    """
    world["arms"] = [_Arm("can0", connected=False, ready=False)]
    world["cams"] = [_Cam("rs:1:color", connected=False)]
    world["apply"]()
    # 발행을 본 적이 없다 — 게이트웨이를 새로 띄운 직후가 이 상태다
    assert _watch().check() == ([], [])


# ── 장치가 빠졌다 ──

def test_one_arm_gone_is_a_usb_problem(world):
    """하나만 사라지면 그 장치의 USB 다."""
    world["arms"] = [_Arm("can0", role="leader"), _Arm("can1", role="follower")]
    world["arm_segs"] = ["can0"]                 # can1 만 사라짐
    world["apply"]()
    new, _ = _watch(world, ["can0", "can1"]).check()
    assert len(new) == 1
    assert new[0].reason == "device_gone" and new[0].ident == "can1"
    assert "USB" in new[0].text


def test_one_camera_gone_names_the_label(world):
    """어느 카메라인지 사람이 알아볼 이름으로 말해야 한다 — `/dev/video4` 로는 모른다."""
    world["cams"] = [_Cam("rs:1:color", label="탑뷰"), _Cam("rs:2:color", label="손목")]
    world["cam_segs"] = ["rs_1_color"]
    world["apply"]()
    new, _ = _watch(world, cams=["rs_1_color", "rs_2_color"]).check()
    assert len(new) == 1 and "손목" in new[0].text


# ── 데몬이 죽은 것과 구분한다 ──

def test_dead_daemon_does_not_blame_usb(world):
    """**회귀** — robotd 를 멈췄더니 먼저 "USB 를 확인하세요"가 떴다.

    데몬 문제인데 사용자를 케이블 뽑으러 보내면 안 된다.
    """
    world["arms"] = [_Arm("can0"), _Arm("can1")]
    world["arm_segs"] = []
    world["apply"]()
    w = _watch(world, ["can0", "can1"])
    world["robotd"] = False
    new, _ = w.check()
    assert [a.reason for a in new] == ["daemon_down"]
    assert "USB" not in new[0].text


def test_all_gone_at_once_names_both_causes(world):
    """전부 한꺼번에 사라지는 건 개별 USB 문제가 아니다.

    생존 표시는 3초 늦게 만료되므로(`DAEMON_ALIVE_TTL_MS`) 그 사이엔 데몬이
    살아 있는 것처럼 보인다 — 실제로 그 창에서 거짓 경보가 났다.
    xHCI 컨트롤러가 죽어도 전부 사라지므로 **둘 다 적어준다.**
    """
    world["arms"] = [_Arm("can0"), _Arm("can1")]
    world["arm_segs"] = []
    world["robotd"] = True                       # 아직 만료 전
    world["apply"]()
    new, _ = _watch(world, ["can0", "can1"]).check()
    assert [a.reason for a in new] == ["all_gone"]
    assert "systemctl" in new[0].text and "컨트롤러" in new[0].text


def test_one_camera_daemon_down_does_not_condemn_the_other(world):
    """rsd 가 죽어도 웹캠은 멀쩡할 수 있다."""
    world["cams"] = [_Cam("rs:1:color"), _Cam("/dev/video2", cam_type="opencv")]
    world["cam_segs"] = ["dev_video2"]
    world["apply"]()
    w = _watch(world, cams=["rs_1_color", "dev_video2"])
    world["rsd"] = False
    new, _ = w.check()
    assert [a.reason for a in new] == ["daemon_down"]
    assert "rsd" in new[0].name


# ── 전이에서만 ──

def test_repeats_are_not_re_announced(world):
    """2초마다 같은 말을 하면 아무도 안 읽는다."""
    world["arms"] = [_Arm("can0"), _Arm("can1")]
    world["arm_segs"] = ["can0"]
    world["apply"]()
    w = _watch(world, ["can0", "can1"])
    assert len(w.check()[0]) == 1
    assert w.check() == ([], [])                 # 두 번째부터는 조용히
    assert len(w.alerts()) == 1                  # 그래도 현재 목록에는 남는다


def test_recovery_is_announced_and_clears(world):
    world["arms"] = [_Arm("can0"), _Arm("can1")]
    world["arm_segs"] = ["can0"]
    world["apply"]()
    w = _watch(world, ["can0", "can1"])
    w.check()
    world["arm_segs"] = ["can0", "can1"]         # 다시 꽂았다
    new, gone = w.check()
    assert new == [] and len(gone) == 1 and gone[0].ident == "can1"
    assert w.alerts() == []


def test_message_text_comes_from_the_backend():
    """화면이 문장을 조립하면 한쪽만 고쳐져 어긋난다 (`usb_warning` 과 같은 규칙)."""
    src = (W.__file__)
    text = open(src).read()
    assert "USB 연결을 확인하세요" in text
    from pathlib import Path

    page = (Path(src).resolve().parents[3]
            / "frontend/src/components/DeviceAlerts.tsx").read_text()
    assert "USB" not in page.split("*/", 1)[-1], "화면이 문구를 직접 만들고 있다"


def test_deliberate_disconnect_is_not_a_disappearance(world):
    """**회귀** — 프리뷰를 끄면 세그먼트가 사라진다. 그걸 "사라졌습니다" 라고 하면
    끌 때마다 경보가 뜬다.

    기억은 세그먼트 이름(`rs_1_color`)인데 호출부는 카메라 id(`rs:1:color`)를 준다 —
    맞춰주지 않아서 실기에서 안 지워졌다.
    """
    world["cams"] = [_Cam("rs:1:color"), _Cam("rs:2:color")]
    world["cam_segs"] = ["rs_1_color"]
    world["apply"]()
    w = _watch(world, cams=["rs_1_color", "rs_2_color"])
    w.forget("camera", "rs:2:color")             # 사용자가 껐다
    assert w.check() == ([], [])


def test_a_single_device_is_not_called_all(world):
    """하나뿐이면 "전부"와 "그 하나"를 구분할 수 없다 — 그 장치의 USB 를 보라는 게 맞다."""
    world["arms"] = [_Arm("can1")]
    world["arm_segs"] = []
    world["apply"]()
    new, _ = _watch(world, ["can1"]).check()
    assert [a.reason for a in new] == ["device_gone"]
