"""camerad — v4l2 데몬 분리 (daemon-inventory.md #3).

rsd 와 **합치지 않는다.** D405 의 UVC 질의가 프로세스를 통째로 먹통으로 만든 전례가
있어서, 합치면 RealSense 가 죽을 때 웹캠까지 죽는다.

여기서 잠그는 것:

1. **소유가 겹치지 않는다** — camerad 는 RealSense 노드를 건너뛰고 rsd 는 v4l2 를 안 본다
2. 두 허브가 **같은 메서드 이름**을 쓴다 — 게이트웨이 분기가 한 줄로 끝난다
3. 게이트웨이는 장치를 열지 않는다
4. 데몬이 게이트웨이를 import 하지 않는다
"""

from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DAEMON = _REPO / "daemons" / "camerad.py"
_V4L2 = _REPO / "cam" / "piper_cam" / "v4l2.py"
_GW = _REPO / "backend" / "app" / "services" / "camera_manager.py"


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            out.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module.split(".")[0])
    return out


@pytest.mark.parametrize("path", [_DAEMON, _V4L2, _REPO / "cam" / "piper_cam" / "hub.py"])
def test_daemon_does_not_import_the_gateway(path):
    assert "app" not in _imports(path), f"{path.name} 이 게이트웨이를 import 한다"


def test_camerad_never_claims_realsense_nodes():
    """**소유가 겹치면 두 데몬이 같은 USB 장치를 두고 싸운다.**

    게이트웨이 시절에는 `rs_available()` 로 조건부였다. 데몬 모델에서는 소유자가
    하나로 정해져 있으므로 무조건 건너뛴다.
    """
    src = _V4L2.read_text()
    assert 'if "realsense" in name.lower():' in src, "RealSense 노드를 무조건 건너뛰지 않는다"
    # **주석이 아니라 호출**을 본다 — 설명문에 옛 이름이 나올 수 있다
    calls = {
        ast.unparse(n.func)
        for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
    }
    assert "rs_available" not in calls, "조건부 소유가 남아 있다"

    # 반대 방향 — rsd 는 v4l2 를 안 본다
    rsd_hub = (_REPO / "rs" / "piper_rs" / "hub.py").read_text()
    assert "VideoCapture" not in rsd_hub, "rsd 가 v4l2 를 연다"


def test_both_hubs_share_one_method_vocabulary():
    """이름이 갈리면 호출부마다 분기가 생기고, 그 분기가 두 번째 진실이 된다."""
    from app.services.realsense_manager import realsense_hub
    from app.services.v4l2_client import v4l2_hub

    for name in ("scan", "connect", "disconnect", "release_all", "probe",
                 "list_controls", "set_control", "has_frame", "get_jpeg"):
        assert callable(getattr(realsense_hub, name)), f"realsense_hub.{name} 없음"
        assert callable(getattr(v4l2_hub, name)), f"v4l2_hub.{name} 없음"


def test_gateway_dispatches_on_cam_type_in_one_place():
    """분기가 흩어지면 새 카메라 종류를 넣을 때마다 여러 곳을 고치게 된다."""
    src = _GW.read_text()
    assert 'cam_type == "realsense"' in src.split("def _hub", 1)[1].split("\n    def ", 1)[0], (
        "_hub 프로퍼티가 분기를 갖고 있지 않다"
    )
    # 장치 종류 분기가 그 한 곳뿐인지
    assert src.count('cam_type == "realsense"') == 1, "분기가 여러 곳에 흩어져 있다"


def test_gateway_no_longer_opens_devices():
    """게이트웨이가 장치를 열면 데몬과 싸운다 — 분리한 의미가 없다."""
    calls = {
        ast.unparse(n.func)
        for n in ast.walk(ast.parse(_GW.read_text())) if isinstance(n, ast.Call)
    }
    for banned in ("VideoCapture", "ioctl", "publish"):
        assert not any(banned in c for c in calls), f"게이트웨이가 {banned} 를 부른다"


def test_gateway_survives_a_dead_daemon(monkeypatch):
    """camerad 가 죽어도 웹은 떠 있어야 한다 — 웹캠만 안 보이는 것으로 격리된다."""
    from app.services import v4l2_client as vc

    class DeadBus:
        def rpc_call(self, *a, **k):
            raise TimeoutError("camerad 없음")

    monkeypatch.setattr(vc, "_bus", lambda: DeadBus())
    hub = vc.V4l2Client()
    assert hub.scan() == []
    assert hub.connect("/dev/video0") == (False, "camerad 연결 실패")
    assert hub.list_controls("/dev/video0") == []


def test_failure_backoff_is_time_based_not_count_based():
    """**회귀** — 실패한 `cap.read()` 는 즉시 돌아온다.

    횟수로 세면 30회가 몇 밀리초 만에 차서 일시적 딸꾹질에도 "사라졌다"가 되고,
    그 사이 루프가 전력으로 돌아 CPU 를 태운다 — 실기에서 camerad 가 하루도 안 돼
    CPU 4시간 41분을 썼다.
    """
    import ast
    import inspect

    from piper_cam.hub import _V4l2Camera

    assert not hasattr(_V4l2Camera, "_MAX_READ_FAILS"), "아직 횟수로 센다"
    assert _V4l2Camera._FAIL_GRACE_S > 0 and _V4l2Camera._FAIL_SLEEP_S > 0

    loop = ast.parse(inspect.getsource(_V4l2Camera._loop).lstrip())
    calls = {ast.unparse(n.func) for n in ast.walk(loop) if isinstance(n, ast.Call)}
    assert "time.sleep" in calls, "실패해도 안 쉰다 — 루프가 폭주한다"


def test_a_camera_that_comes_back_is_not_lost_anymore(monkeypatch):
    """⚠ **실기 사고(2026-10-06)**: Global Shutter 카메라가 다른 앱에서는 잘 나오는데
    여기서는 계속 "연결 안 됨"이었다. camerad 가 오전에 한 번 "잃어버림"으로 판정한 뒤
    **[끊기] 말고는 그 판정을 지울 길이 없었다.** 다시 꽂혀 스캔에 보여도, 다시 연결돼
    프레임이 나와도 `lost()` 에 남았고, 게이트웨이의 장치 감시가 그걸 보고 주기마다
    "없음"으로 되돌렸다. so101d·rsd 는 같은 병을 이미 고쳤었다(1b036f0).
    """
    from piper_cam import hub as H

    monkeypatch.setattr(H.v4l2, "scan_cameras",
                        lambda: [{"id": "/dev/video12", "name": "Global Shutter Camera"}])
    h = H.V4l2Hub()
    h.scan()
    h.cams["/dev/video12"].lost_at = 1791265234.0          # 오전에 잃어버림 판정
    assert h.lost(), "전제: 잃어버림으로 들고 있다"

    h.scan()                                               # 다시 꽂혀 스캔에 보인다
    assert h.lost() == [], "다시 보이는데 아직 잃어버림이다 — 장치 감시가 또 지운다"


def test_a_running_camera_keeps_its_own_verdict(monkeypatch):
    """돌고 있는 카메라의 판정은 **읽기 루프**가 한다 — 스캔이 덮으면 안 된다."""
    from piper_cam import hub as H

    monkeypatch.setattr(H.v4l2, "scan_cameras", lambda: [{"id": "/dev/video3", "name": "x"}])
    h = H.V4l2Hub()
    h.scan()
    cam = h.cams["/dev/video3"]
    cam.lost_at = 123.0
    monkeypatch.setattr(type(cam), "connected", property(lambda self: True))
    h.scan()
    assert cam.lost_at == 123.0


def test_a_successful_reconnect_clears_the_lost_verdict():
    """다시 열렸으면 예전 "잃어버림" 판정은 거짓이다."""
    src = (REPO / "cam" / "piper_cam" / "hub.py").read_text()
    after_open = src.split("Cannot open {self.id}", 1)[1][:200]
    assert "self.lost_at = 0.0" in after_open, "연결에 성공해도 잃어버림이 남는다"


# ── 캡처 모드 — 화각을 지키며 데이터셋 크기를 맞춘다 (2026-10-06) ──


def _cam_with(width, height):
    from piper_cam import hub as H

    c = H._V4l2Camera("/dev/video12")
    c.width, c.height = width, height
    return c


def test_a_wide_capture_is_shrunk_to_the_requested_size_without_losing_the_view():
    """⚠ AR0234(Global Shutter) 실측: 640×480 모드는 센서 가운데 1/3 만 내보낸다.
    1280×960 으로 받아 640×480 으로 **줄이면** 데이터셋 크기는 같고 화각은 넓다.
    여기서는 가장자리 표지가 줄인 뒤에도 남는지로 "잘리지 않았다"를 본다."""
    import numpy as np

    f = np.zeros((960, 1280, 3), np.uint8)
    f[:, :8] = 255                                # 왼쪽 끝 표지
    out = _cam_with(640, 480)._shape(f)
    assert out.shape[:2] == (480, 640)
    assert out[:, :2].mean() > 100, "왼쪽 끝이 사라졌다 — 줄인 게 아니라 잘랐다"


def test_a_different_aspect_is_cropped_to_fit_not_stretched():
    """1920×1200(16:10) → 640×480(4:3): 늘이면 물체가 찌그러지고 학습이 그걸 배운다.
    가운데를 4:3 으로 잘라 맞춘 뒤 줄인다 — 좌우 끝만 잃는다."""
    import numpy as np

    f = np.zeros((1200, 1920, 3), np.uint8)
    f[:, :100] = 255                              # 잘려 나갈 왼쪽 띠(160px 안쪽)
    f[600, 960] = 255
    out = _cam_with(640, 480)._shape(f)
    assert out.shape[:2] == (480, 640)
    assert out[:, :4].mean() < 5, "좌우를 안 자르고 늘였다"


def test_without_a_capture_mode_frames_pass_untouched():
    import numpy as np

    f = np.zeros((480, 640, 3), np.uint8)
    assert _cam_with(640, 480)._shape(f) is f


def test_changing_the_capture_mode_reopens_the_device(monkeypatch):
    """저장만 되고 그대로 돌면 "설정했는데 안 바뀐다" 가 된다."""
    from piper_cam import hub as H

    c = H._V4l2Camera("/dev/video12")
    c._cap = object()                             # 열려 있다
    c._want = (640, 480, 30)
    closed = []
    monkeypatch.setattr(c, "disconnect", lambda: (closed.append(1), setattr(c, "_cap", None)))
    monkeypatch.setattr(c, "_open", lambda: (None, None))
    c.connect((640, 480, 30), (1280, 960))
    assert closed and c._capture == (1280, 960)


def test_the_capture_mode_is_parsed_strictly_and_survives_a_restart(tmp_path):
    """사람이 적은 값이 조용히 무시되면 "설정했는데 화각이 그대로" 가 된다."""
    from app.services.camera_manager import parse_capture

    assert parse_capture("1280x960") == [1280, 960]
    assert parse_capture("1280 × 960") == [1280, 960]
    assert parse_capture([1920, 1200]) == [1920, 1200]
    for off in (None, "", []):
        assert parse_capture(off) is None
    for bad in ("1280", "wide", "0x0"):
        try:
            parse_capture(bad)
        except ValueError:
            continue
        raise AssertionError(f"틀린 값을 받아들였다: {bad!r}")

    src = (REPO / "backend" / "app" / "services" / "camera_manager.py").read_text()
    assert '"capture": cam.capture' in src, "세션에 안 남는다 — 재시작하면 화각이 다시 좁아진다"
    assert 'cam.capture = parse_capture(cam_data.get("capture"))' in src
