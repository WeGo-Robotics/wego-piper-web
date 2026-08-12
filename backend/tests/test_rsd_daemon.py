"""rsd — RealSense 데몬 분리 (daemon-inventory.md #4, daemon-split 5단계).

## 왜 따로 뗐나

D405 의 UVC 컨트롤 질의가 커널 D-state 로 **이벤트 루프 전체를 먹통**으로 만든
전례가 있다. 게이트웨이 안에 있으면 그 순간 웹도 같이 멈춘다.
그래서 camerad(v4l2)와도 합치지 않고 따로 둔다.

여기서 잠그는 것:

1. **데몬이 게이트웨이를 import 하지 않는다** — 하면 분리한 의미가 없다
2. **프레임은 RPC 로 다니지 않는다** — 픽셀 요청/응답은 옛 base64-JPEG 왕복이다
3. **게이트웨이 공개 인터페이스가 그대로다** — 라우터가 안 바뀌어야 한다
4. rsd 가 죽어도 게이트웨이는 산다
"""

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DAEMON = _REPO / "daemons" / "rsd.py"
_HUB = _REPO / "rs" / "piper_rs" / "hub.py"
_CLIENT = _REPO / "backend" / "app" / "services" / "realsense_manager.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module.split(".")[0])
    return out


@pytest.mark.parametrize("path", [_DAEMON, _HUB])
def test_daemon_does_not_import_the_gateway(path):
    """데몬이 백엔드를 import 하면 프로세스만 나뉘고 결합은 그대로다."""
    assert "app" not in _imports(path), f"{path.name} 이 게이트웨이를 import 한다"


def test_frames_do_not_travel_over_rpc():
    """**픽셀은 shm, 제어는 버스.** 프레임 RPC 를 만들면 옛 base64-JPEG 왕복이 된다."""
    src = _DAEMON.read_text()
    methods = ast.literal_eval(
        src.split("_METHODS = ", 1)[1].split("\n\n", 1)[0].strip()
    )
    for banned in ("get_frame", "get_jpeg", "has_frame"):
        assert banned not in methods, f"프레임을 RPC 로 나르고 있다: {banned}"

    # 게이트웨이는 그 둘을 세그먼트에서 직접 읽어야 한다
    client = _CLIENT.read_text()
    for fn in ("def has_frame", "def get_jpeg"):
        body = client.split(fn, 1)[1].split("\n    def ", 1)[0]
        assert "Subscriber" in body, f"{fn} 이 shm 을 안 읽는다"
        assert "rpc_call" not in body and "_call(" not in body, f"{fn} 이 RPC 를 쓴다"


def test_gateway_keeps_the_old_public_interface():
    """라우터가 그대로여야 한다 — 브리지를 Redis 로 갈아끼울 때와 같은 방식이다."""
    from app.services.realsense_manager import realsense_hub

    for name in ("scan", "connect", "disconnect", "release_all", "is_d405",
                 "probe", "hardware_reset", "list_controls", "set_control",
                 "has_frame", "get_jpeg"):
        assert callable(getattr(realsense_hub, name)), f"realsense_hub.{name} 없음"


def test_gateway_survives_a_dead_daemon(monkeypatch):
    """**rsd 가 죽어도 웹은 떠 있어야 한다** — 카메라만 안 보이는 것으로 격리된다."""
    from app.services import realsense_manager as rm

    class DeadBus:
        def rpc_call(self, *a, **k):
            raise TimeoutError("rsd 없음")

    monkeypatch.setattr(rm, "_bus", lambda: DeadBus())
    hub = rm.RealSenseHub()
    assert hub.scan() == []                       # 예외가 새면 카메라 페이지가 500 이 된다
    assert hub.connect("rs:1:color") == (False, "rsd 연결 실패")
    assert hub.list_controls("rs:1:color") == []
    assert hub.release_all() is False


def test_publishers_stop_after_the_read_thread():
    """**순서 버그** — 발행자를 먼저 닫으면 루프가 한 프레임 더 발행해 세그먼트를 되살린다.

    그러면 소비자가 발행자 없는 세그먼트를 열어 "멈춘 화면"을 본다.
    실기에서 실제로 재현했다.
    """
    src = _HUB.read_text()
    body = src.split("def _stop_pipeline", 1)[1].split("\n    def ", 1)[0]
    stop_thread = body.index("self._running = False")
    stop_pub = body.index("stop_publish(f\"rs:")
    assert stop_thread < stop_pub, "발행자를 스레드보다 먼저 닫는다 — 세그먼트가 되살아난다"


def test_daemon_scans_at_startup():
    """스캔을 안 하면 장치 목록이 비어 `connect` 가 "not found" 로 실패한다.

    게이트웨이 시절에는 카메라 페이지가 스캔을 먼저 불러 가려져 있던 순서 의존이다.
    """
    src = _DAEMON.read_text()
    main = src.split("def main(", 1)[1]
    assert "hub.scan()" in main.split("serve(bus, hub)", 1)[0], "기동 시 스캔하지 않는다"
