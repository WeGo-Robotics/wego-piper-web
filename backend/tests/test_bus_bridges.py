"""브리지 3개의 Redis 전환 검증 (refactor/daemon-split.md 3단계).

ZMQ 소켓 3개(5555 파라미터 / 5556 프리뷰 / 5557 녹화제어)를 Redis 로 바꿨다.
**프로세스 경계는 그대로고 전송만 바뀌었다** — 그래서 여기서 잠그는 것은
"기능이 늘었는가"가 아니라 **"바뀐 전송이 옛 시맨틱을 유지하는가"** 다.

## 가장 중요한 회귀: 세션 격리

ZMQ 는 소켓을 닫으면 큐도 사라졌다. **Redis 리스트는 살아남는다.**
안 비우면 지난 세션 끝에 보낸 명령·파라미터가 다음 세션 시작 직후에 적용된다 —
"이전 녹화에서 누른 건너뛰기가 새 녹화의 첫 에피소드를 날린다"가 된다.
"""

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


# ── Redis 없이 도는 계약 검사 ────────────────────────────────────────────────

# 전송을 바꾼 파일들. 여기 zmq 가 남아 있으면 교체가 반쪽이라는 뜻이다.
_SWITCHED = [
    "backend/app/services/param_bridge.py",
    "backend/app/services/preview_bridge.py",
    "backend/app/services/control_bridge.py",
    "wrapper/lerobot_wrapper.py",
    "wrapper/grpc_wrapper.py",
    "wrapper/start_record.py",
]


def _imported_modules(path: Path) -> set[str]:
    """AST 로 import 를 본다 — 문자열·주석에 'zmq' 가 있어도 오탐하지 않는다."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("rel", _SWITCHED)
def test_transport_paths_no_longer_import_zmq(rel):
    """전송을 바꾼 파일에 zmq 가 남으면 안 된다.

    두 전송이 공존하면 어느 쪽이 실제로 쓰이는지 알 수 없고,
    한쪽만 고친 채 "동작한다"고 착각하게 된다.
    """
    assert "zmq" not in _imported_modules(_REPO / rel), f"{rel} 이 아직 zmq 를 import 한다"


@pytest.mark.parametrize("rel", _SWITCHED)
def test_transport_paths_use_the_contract_package(rel):
    """토픽·키 이름을 각자 적지 않고 `piper_bus` 에서 가져온다."""
    assert "piper_bus" in _imported_modules(_REPO / rel), f"{rel} 이 piper_bus 를 안 쓴다"


def test_settings_no_longer_carry_three_zmq_addresses():
    """주소가 3개에서 1개(`redis_url`)로 줄었다."""
    from app.core.config import settings

    for gone in ("zmq_address", "preview_zmq_address", "control_zmq_address"):
        assert not hasattr(settings, gone), f"{gone} 가 아직 남아 있다"
    assert hasattr(settings, "redis_url")


def test_bridge_public_interfaces_unchanged():
    """라우터 9곳이 이 이름들을 부른다 — 바뀌면 조용히 깨진다.

    이번 작업의 전제가 "경계는 그대로, 전송만"이므로 인터페이스를 여기서 못 박는다.
    """
    from app.services.control_bridge import control_bridge
    from app.services.param_bridge import param_bridge
    from app.services.preview_bridge import preview_bridge

    for name in ("connect", "close", "validate_params", "send_params", "clear"):
        assert callable(getattr(param_bridge, name)), f"param_bridge.{name} 없음"
    for name in ("start", "stop", "get", "names"):
        assert callable(getattr(preview_bridge, name)), f"preview_bridge.{name} 없음"
    for name in ("start", "stop", "send"):
        assert callable(getattr(control_bridge, name)), f"control_bridge.{name} 없음"


def test_control_commands_come_from_the_contract():
    """명령 문자열이 백엔드와 wrapper 양쪽에 따로 적히지 않게 한다.

    `_ERR_BITS` 가 프로세스 경계를 넘어 복붙됐던 사고(refactor/04-err-bits.md)의 재발 방지.
    """
    from piper_bus import contract as C

    assert C.CONTROL_COMMANDS == {C.CONTROL_SKIP, C.CONTROL_RERECORD, C.CONTROL_STOP}
    # wrapper 가 리터럴 대신 계약 상수를 쓰는지
    src = (_REPO / "wrapper" / "start_record.py").read_text()
    assert "C.CONTROL_SKIP" in src and "C.CONTROL_STOP" in src
    # record_manager 가 보내는 키가 계약에 있는 명령인지
    rm = (_REPO / "backend" / "app" / "services" / "record_manager.py").read_text()
    for cmd in C.CONTROL_COMMANDS:
        assert cmd in rm, f"record_manager 가 {cmd!r} 를 다루지 않는다"


def test_queues_are_cleared_before_the_wrapper_is_launched():
    """**순서가 하중을 받는다.** 큐 비우기가 wrapper 기동보다 먼저여야 한다.

    `clear_control()` 은 이미 `BRPOP` 으로 대기 중인 소비자에게 **배달된 명령을
    회수하지 못한다** — 그게 블로킹 pop 의 성질이다. 지금 안전한 유일한 이유는
    비울 때 소비자가 아직 존재하지 않기 때문이다.

    `control_bridge.start()` 를 `record_manager.start()` 뒤로 옮기면 무해해 보이지만,
    지난 세션의 `escape` 가 새 녹화의 첫 에피소드를 즉시 정지시킨다.
    (2프로세스 시뮬레이션에서 실제로 재현했다.)
    """
    src = (_REPO / "backend" / "app" / "routers" / "recording.py").read_text()
    tree = ast.parse(src)

    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(c, ast.Attribute) and c.attr == "start"
            and isinstance(c.value, ast.Name) and c.value.id == "control_bridge"
            for c in ast.walk(n)
        )
    )

    def line_of(obj: str, attr: str) -> int:
        for node in ast.walk(fn):
            if (isinstance(node, ast.Attribute) and node.attr == attr
                    and isinstance(node.value, ast.Name) and node.value.id == obj):
                return node.lineno
        raise AssertionError(f"{obj}.{attr} 호출을 못 찾았다")

    assert line_of("control_bridge", "start") < line_of("record_manager", "start"), \
        "제어 큐를 비우기 전에 wrapper 가 뜨면 지난 세션 명령이 배달된다"
    assert line_of("preview_bridge", "start") < line_of("record_manager", "start"), \
        "프리뷰를 비우기 전에 wrapper 가 뜨면 지난 녹화의 마지막 화면이 남는다"


def test_preview_key_roundtrip():
    from piper_bus import contract as C

    for name in ("top", "wrist.cam", "observation.images.side"):
        assert C.preview_name(C.preview_key(name)) == name


# ── Redis 가 있어야 도는 것 ──────────────────────────────────────────────────

pytest.importorskip("redis")
from piper_bus import Bus, contract as C  # noqa: E402


@pytest.fixture
def bus():
    b = Bus()
    if not b.ping():
        pytest.skip("Redis 미실행")
    _wipe(b)
    yield b
    _wipe(b)


def _wipe(b: Bus) -> None:
    b.r.delete(C.PARAMS_QUEUE, C.RECORD_CONTROL_QUEUE)
    b.clear_previews()


def test_params_queue_is_fifo(bus):
    """PUSH/PULL 이 큐였으므로 순서가 유지돼야 한다."""
    bus.push_params({"n": 1})
    bus.push_params({"n": 2})
    assert bus.pop_params()["n"] == 1
    assert bus.pop_params()["n"] == 2


def test_pop_returns_none_when_empty_instead_of_raising(bus):
    """`socket_timeout` 이 BRPOP 대기보다 짧으면 redis-py 가 TimeoutError 를 던진다.

    소비 루프가 그걸로 죽으면 파라미터 변경이 통째로 멈춘다. "빈 큐"로 흡수해야 한다.
    """
    assert bus.pop_params(timeout=1) is None
    assert bus.pop_control(timeout=1) is None


def test_clear_params_drops_previous_session(bus):
    """**핵심 회귀** — 지난 세션 파라미터가 다음 추론에 새면 안 된다.

    ZMQ 는 소켓을 닫으면 큐가 사라졌다. Redis 리스트는 안 사라진다.
    """
    bus.push_params({"max_velocity": 500})
    assert bus.clear_params() == 1
    assert bus.pop_params(timeout=1) is None


def test_control_bridge_clears_queue_on_start_and_stop(bus):
    """**핵심 회귀** — 이전 녹화의 명령이 새 녹화 첫 에피소드를 날리면 안 된다."""
    from app.services.control_bridge import ControlBridge

    bus.push_control(C.CONTROL_STOP)          # 지난 세션의 잔여 명령
    cb = ControlBridge(bus)

    cb.start()
    assert bus.pop_control(timeout=1) is None, "start() 가 이전 명령을 안 버렸다"

    assert cb.send(C.CONTROL_SKIP) is True
    cb.stop()
    assert bus.pop_control(timeout=1) is None, "stop() 이 큐를 안 비웠다"


def test_control_bridge_is_noop_when_not_recording(bus):
    """녹화 중이 아니면 no-op — ZMQ 시절 '소켓이 없으면 못 보낸다'와 같은 동작."""
    from app.services.control_bridge import ControlBridge

    cb = ControlBridge(bus)
    assert cb.send(C.CONTROL_SKIP) is False
    assert bus.pop_control(timeout=1) is None


def test_control_bridge_rejects_unknown_command(bus):
    from app.services.control_bridge import ControlBridge

    cb = ControlBridge(bus)
    cb.start()
    assert cb.send("enter") is False
    assert bus.pop_control(timeout=1) is None


def test_preview_keeps_only_the_latest_frame(bus):
    """**프리뷰만 큐가 아니다.** 큐면 UI 폴링이 밀릴 때 JPEG 가 무한히 쌓인다."""
    bus.put_preview("top", b"\xff\xd8old")
    bus.put_preview("top", b"\xff\xd8new")
    assert bus.get_preview("top") == b"\xff\xd8new"
    assert bus.preview_names() == ["top"]


def test_preview_survives_binary_roundtrip(bus):
    """JPEG 는 디코드하면 깨진다 — 프리뷰는 바이너리 클라이언트를 써야 한다."""
    jpeg = bytes(range(256)) * 4          # utf-8 로 디코드 불가능한 바이트열
    bus.put_preview("wrist", jpeg)
    assert bus.get_preview("wrist") == jpeg


def test_preview_has_ttl_so_stale_frames_disappear(bus):
    """옛 `_FRESH_SECONDS` 판정을 TTL 이 대신한다 — 만료를 Redis 가 처리한다."""
    bus.put_preview("top", b"x")
    ttl = bus.r.pttl(C.preview_key("top"))
    assert 0 < ttl <= C.PREVIEW_TTL_MS


def test_preview_bridge_clears_previous_session(bus):
    """새 녹화 시작 시 지난 녹화의 마지막 화면이 남으면 안 된다."""
    from app.services.preview_bridge import PreviewBridge

    bus.put_preview("top", b"old-session")
    pb = PreviewBridge(bus)
    pb.start()
    assert pb.names() == []
    assert pb.get("top") is None
