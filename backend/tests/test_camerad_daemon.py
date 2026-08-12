"""camerad — v4l2 데몬 분리 (daemon-inventory.md #3).

rsd 와 **합치지 않는다.** D405 의 UVC 질의가 프로세스를 통째로 먹통으로 만든 전례가
있어서, 합치면 RealSense 가 죽을 때 웹캠까지 죽는다.

여기서 잠그는 것:

1. **소유가 겹치지 않는다** — camerad 는 RealSense 노드를 건너뛰고 rsd 는 v4l2 를 안 본다
2. 두 허브가 **같은 메서드 이름**을 쓴다 — 게이트웨이 분기가 한 줄로 끝난다
3. 게이트웨이는 장치를 열지 않는다
4. 데몬이 게이트웨이를 import 하지 않는다
"""

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
