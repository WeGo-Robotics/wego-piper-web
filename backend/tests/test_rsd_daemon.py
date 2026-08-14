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


# ── 스캔 썸네일 ──────────────────────────────────────────────────────────────

def test_blocking_pop_waits_the_full_timeout():
    """**회귀** — `socket_timeout`(1초) 때문에 모든 블로킹 대기가 1초에 끊겼다.

    `_brpop` 이 소켓 타임아웃을 "빈 큐"로 흡수하면서, 아무리 긴 타임아웃을 줘도
    1초 만에 포기했다. `probe` 가 40초를 요청하고 1초에 "응답 없음"으로 실패했다.
    소켓 타임아웃 자체는 없애면 안 된다 — 느린 Redis 가 이벤트 루프를 멈추면
    heartbeat 이 끊겨 E-stop 이 돈다.
    """
    import inspect

    from piper_bus.client import Bus

    src = inspect.getsource(Bus._brpop)
    assert "deadline" in src, "마감까지 반복하지 않는다"
    assert "continue" in src, "소켓 타임아웃에서 그냥 포기한다"


def test_probe_keeps_the_thumbnail_segment():
    """probe 가 세그먼트를 지우면 스캔 화면에 아무것도 안 남는다.

    probe 는 스트림을 잠깐 켜서 한 장 얻고 되돌리는데, 그때 세그먼트까지 지우면
    썸네일이 사라진다. 실제로 6대 전부 프리뷰가 안 보였다.
    """
    src = _HUB.read_text()
    assert "_stop_pipeline(unlink_segments=False)" in src, (
        "probe 되돌리기가 썸네일 세그먼트를 지운다"
    )
    # 반대로 명시적 해제는 지워야 한다 — 안 지우면 "멈춘 화면"이 남는다
    assert "def _stop_pipeline(self, unlink_segments: bool = True)" in src


def test_probe_settles_before_keeping_the_frame():
    """RealSense 첫 프레임은 자동노출이 안 잡혀 **까맣다.**

    그걸 썸네일로 남기면 화면이 검게 보인다 (실측 평균밝기 0.5 → 안정화 후 13.1).
    """
    src = _HUB.read_text()
    assert "SETTLE_S" in src, "안정화 대기가 없다"
    body = src.split("def probe_stream", 1)[1].split("\n    def ", 1)[0]
    assert "time.sleep(self.SETTLE_S)" in body, "첫 프레임을 그대로 쓴다"


def test_probe_enables_all_streams_of_a_device_at_once():
    """스캔은 스트림마다 probe 를 부른다 — 하나씩 켜면 기동·안정화가 곱해진다.

    실측 6스트림 17초 → 장치 단위로 묶고 재사용 창을 두어 6초.
    """
    src = _HUB.read_text()
    assert "self._active | self.available" in src, "장치의 스트림을 함께 켜지 않는다"
    assert "PROBE_REUSE_S" in src, "최근 probe 결과를 재사용하지 않는다"


# ── 장치가 사라진 것을 rsd 가 스스로 판정하는가 ─────────────────────────────

def test_read_errors_reach_a_verdict():
    """**회귀** — "리얼센스 뽑으니 바로 알람 안 온다".

    장치를 뽑으면 librealsense 는 **예외를 던진다.** 예전 루프는 그걸
    `except` 에서 삼키고 0.1초 자며 영원히 돌았다 — 빈 프레임 카운터가 안 늘어
    판정에 **도달할 수가 없었다.** 그래서 아무 일도 안 일어났다.
    """
    from piper_rs.hub import _RSDevice

    dev = _RSDevice("S1", "D405", "", {"color"})     # 포트 모름 → 존재 판정은 건너뛴다

    class _Boom:
        def try_wait_for_frames(self, timeout_ms=0):
            raise RuntimeError("No device connected")

    verdicts = []
    dev._pipeline = _Boom()
    dev._running = True
    dev._stop_pipeline = lambda: None
    dev._declare_lost = lambda why="": verdicts.append(why) or setattr(dev, "_running", False)

    dev._read_loop()
    assert verdicts, "예외가 계속 나는데 결론을 안 낸다"
    assert "실패" in verdicts[0]


def test_usb_node_removal_is_the_decisive_signal():
    """뽑으면 `/sys/bus/usb/devices/<포트>` 가 즉시 사라진다.

    camerad 의 `/dev/videoN`, robotd 의 `/sys/class/net/can0` 과 같은 신호다.
    **librealsense 에 다시 묻지 않는다** — `query_devices()` 를 주기적으로 부르면
    D405 를 D-state 로 물리게 하는 그 질의를 늘리는 셈이다.
    """
    from piper_rs.hub import _RSDevice

    assert _RSDevice("S", "D405", "9-99:1.0", set())._device_present() is False
    # 포트를 모르면 판정하지 않는다 — 모르는 것과 없는 것은 다르다
    assert _RSDevice("S", "D405", "", set())._device_present() is True


def test_presence_check_does_not_query_librealsense():
    """존재 확인이 장치를 건드리면 격리를 스스로 깨는 것이다."""
    import ast
    import inspect

    from piper_rs.hub import _RSDevice

    src = inspect.getsource(_RSDevice._device_present)
    calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(src.lstrip()))
             if isinstance(n, ast.Call)}
    assert not any("rs." in c or "query_devices" in c for c in calls), \
        f"librealsense 를 부른다: {calls}"
