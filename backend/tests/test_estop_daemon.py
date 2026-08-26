"""E-stop 독립 프로세스 (refactor/daemon-inventory.md #1, daemon-split 2단계).

CLAUDE.md / REF.md 가 *"E-stop 은 웹서버와 반드시 분리된 독립 watchdog 프로세스"* 를
설계 원칙으로 못 박았지만, 실제로는 `asyncio.create_task` 로 게이트웨이와 **같은
이벤트 루프**에서 돌았다. 루프가 막히면 워치독도 멈춘다 —
D405 UVC hang 때 실제로 그랬고, 그 순간 E-stop 은 죽어 있었다.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_ESTOPD = _REPO / "daemons" / "estopd.py"

pytest.importorskip("redis")
from piper_bus import Bus, contract as C  # noqa: E402

# Redis 격리는 `conftest.py` 의 autouse 픽스처가 한다 — 파일마다 반복하지 않는다.


@pytest.fixture
def bus():
    b = Bus()
    if not b.ping():
        pytest.skip("Redis 미실행")
    b.r.delete(C.ESTOP_HEARTBEAT, C.ESTOP_ARMED, C.ESTOP_LAST, C.ACTIVITY_PIDS)
    yield b
    b.r.delete(C.ESTOP_HEARTBEAT, C.ESTOP_ARMED, C.ESTOP_LAST, C.ACTIVITY_PIDS)


@pytest.fixture
def victim():
    """죽어야 할 가짜 '추론 프로세스'."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield p
    if p.poll() is None:
        p.kill()
    p.wait(timeout=5)


@pytest.fixture
def estopd():
    procs = []

    def _start(timeout_s: float = 1.0, poll_s: float = 0.1):
        env = {**os.environ,
               "PIPER_ESTOP_TIMEOUT_S": str(timeout_s),
               "PIPER_ESTOP_POLL_S": str(poll_s)}
        p = subprocess.Popen([sys.executable, str(_ESTOPD)], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        time.sleep(0.5)  # 기동 대기
        return p

    yield _start
    for p in procs:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def _wait_dead(p: subprocess.Popen, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if p.poll() is not None:
            return True
        time.sleep(0.05)
    return False


def test_daemon_is_a_separate_file_not_an_asyncio_task():
    """in-process 워치독으로 되돌아가지 않게 한다."""
    assert _ESTOPD.exists(), "daemons/estopd.py 가 없다"
    import ast

    tree = ast.parse(_ESTOPD.read_text())
    imported = {
        (a.name.split(".")[0])
        for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    } | {
        n.module.split(".")[0]
        for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert "asyncio" not in imported, "estopd 가 이벤트 루프에 의존하면 안 된다"
    assert "SIGKILL" in _ESTOPD.read_text(), "E-stop 은 graceful stop 이 아니다"


def test_heartbeat_keeps_process_alive(bus, victim, estopd):
    estopd(timeout_s=1.0)
    bus.set_activity_pid("inference", victim.pid)
    bus.set_armed(True)
    for _ in range(10):
        bus.beat()
        time.sleep(0.15)
    assert victim.poll() is None, "heartbeat 가 있는데 죽었다"


def test_heartbeat_timeout_kills_the_process(bus, victim, estopd):
    estopd(timeout_s=1.0)
    bus.set_activity_pid("inference", victim.pid)
    bus.set_armed(True)
    bus.beat()
    assert _wait_dead(victim, 4.0), "heartbeat 가 끊겼는데 안 죽었다"

    last = bus.last_estop()
    assert last and last["reason"] == C.ESTOP_REASON_TIMEOUT
    assert "inference" in last["stopped"]
    # 정지 후에는 감시를 끄고 PID 를 지운다 (같은 PID 를 재사용하는 사고 방지)
    assert bus.is_armed() is False
    assert bus.activity_pids() == {}


def test_survives_frozen_gateway(bus, victim, estopd):
    """⚠ **이 테스트가 이 리팩터의 전부다.**

    게이트웨이 이벤트 루프가 통째로 멈춘 상태(SIGSTOP = D-state 흉내)에서도
    팔이 서야 한다. in-process 워치독이면 여기서 실패한다.
    """
    gateway = subprocess.Popen([sys.executable, "-c", f"""
import time
from piper_bus import Bus
b = Bus()
b.set_activity_pid("inference", {victim.pid}); b.set_armed(True)
while True:
    b.beat(); time.sleep(0.1)
"""])
    try:
        time.sleep(0.8)
        estopd(timeout_s=1.0)
        time.sleep(1.2)
        assert victim.poll() is None, "정상 동작 중인데 죽었다"

        os.kill(gateway.pid, signal.SIGSTOP)  # 게이트웨이 완전 정지
        assert _wait_dead(victim, 4.0), "게이트웨이가 멈추자 E-stop 도 같이 멈췄다"
        assert bus.last_estop()["reason"] == C.ESTOP_REASON_TIMEOUT
    finally:
        try:
            os.kill(gateway.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        gateway.kill()
        gateway.wait(timeout=5)


def test_not_armed_means_no_kill(bus, victim, estopd):
    """로봇을 움직이는 활동이 없으면 감시하지 않는다 — 오탐 방지."""
    estopd(timeout_s=0.5)
    bus.set_activity_pid("inference", victim.pid)
    bus.set_armed(False)
    time.sleep(2.0)
    assert victim.poll() is None, "armed 가 아닌데 죽였다"


def test_gateway_survives_without_bus(monkeypatch):
    """Redis 가 없어도 게이트웨이는 떠야 한다 (bus_available=False 로 알린다)."""
    from piper_bus import client as bus_client

    from app.services.estop_bridge import EstopBridge

    # `isolated_redis` 가 이미 `url()` 을 DB 15 로 고정했으므로 env 만으로는 안 된다.
    # 같은 지점을 덮어써야 "버스가 없는" 상황이 실제로 재현된다.
    dead = "redis://127.0.0.1:1/0"  # 없는 포트
    monkeypatch.setenv("PIPER_REDIS_URL", dead)
    monkeypatch.setattr(bus_client, "url", lambda: dead)
    b = EstopBridge()
    assert b.connect() is False
    b.heartbeat()          # 예외 없이 no-op
    b.sync_activities()
    assert b.status()["bus_available"] is False


def test_the_test_bus_cannot_reach_the_real_robot():
    """⚠ **테스트가 실기 팔의 토크를 끊고 있었다.**

    격리를 Redis db 번호(15)로만 했는데, **pub/sub 은 db 를 안 가린다** —
    db 15 에 `publish` 한 E-stop 을 db 0 의 robotd 가 받아서 실제로 팔을 세웠다.
    테스트를 한 번 돌릴 때마다 로봇이 서고, 리더암은 마스터 모드까지 풀렸다.

    그래서 격리 기준을 **이름 접두사**로 옮겼다. 채널과 키가 같은 규칙을 쓰므로
    한 번에 갈라지고, 자식 프로세스도 환경변수로 따라온다.
    """
    assert not C.CH_ESTOP.startswith("piper:"), \
        f"테스트가 운영 채널({C.CH_ESTOP})로 발행한다 — 실기가 선다"
    assert not C.ESTOP_ARMED.startswith("piper:"), \
        f"테스트가 운영 키({C.ESTOP_ARMED})를 만진다"


def test_the_prefix_is_fixed_before_the_contract_loads():
    """`PREFIX` 는 모듈 로드 때 정해진다 — fixture 로 바꾸면 **이미 늦다.**

    늦게 바꾸면 조용히 운영 이름으로 돌아가므로, 접두사가 실제로 먹었는지
    본다(위 검사)와 별개로 설정 지점이 import 전인지도 못박는다.
    """
    conftest = (Path(__file__).parent / "conftest.py").read_text()
    before = conftest.split("import pytest", 1)[0] + conftest.split("import pytest", 1)[1]
    idx_env = before.index('os.environ["PIPER_BUS_PREFIX"]')
    idx_import = before.find("from piper_bus")
    assert idx_import == -1 or idx_env < idx_import, \
        "piper_bus 를 import 한 뒤에 접두사를 박는다 — 안 먹는다"
