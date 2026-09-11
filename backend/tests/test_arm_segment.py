"""팔 shm 세그먼트 — 포맷 계약 (refactor/robot-transport.md 2단계).

카메라와 다른 점만 집중해서 본다: **양방향**이고, 레코드는 이름표 없이 값만 담는다.
그래서 순서가 틀리면 어떤 에러도 안 나고 **그럴듯한 쓰레기 자세**가 나온다.
"""

import struct
import time

import pytest

pytest.importorskip("piper_shm")
from piper_shm import (  # noqa: E402
    JOINTS,
    ActionReader,
    ActionWriter,
    ArmSegmentError,
    StateReader,
    StateWriter,
)
from piper_shm import arm as A  # noqa: E402

IFACE = "pytest-can"
POSE = {j: float(i * 10 - 30) for i, j in enumerate(JOINTS)}


@pytest.fixture(autouse=True)
def _clean():
    yield
    for kind in (A.KIND_STATE, A.KIND_ACTION):
        A.unlink(A.segment_name(IFACE, kind))


# 관절 순서가 상류 모터 버스와 같은지는 `test_robot_transport.py` 가 본다 —
# 거기는 **설치된** 패키지를 읽는다. `vendor/lerobot_robot_piper/` 는 별도
# 저장소의 사본이라 설치되지 않고, 그걸 읽으면 안 도는 코드를 검사하게 된다.


def test_a_writer_makes_the_segment_readable_by_other_users():
    """⚠ NUC 처음 설치(2026-09-11): 팔로워 action 은 게이트웨이 **컨테이너(root)** 가 만들고
    호스트의 simd(일반 사용자)가 읽는다 — 0600 이면 `Permission denied` 로 못 열어 팔이
    영영 안 움직였다. umask 가 무엇이든 만든 세그먼트는 0644 여야 하고, 쓰기는 소유자만이다.
    (호스트가 먼저 만들어 두는 우회는 `fs.protected_regular` 가 root 의 O_CREAT 를 막아
    안 된다 — 만드는 쪽이 곧 쓰는 쪽이어야 한다.)"""
    import os

    old = os.umask(0o077)
    try:
        w = A.ActionWriter(IFACE, deadman_ms=500)
    finally:
        os.umask(old)
    try:
        mode = os.stat(A.segment_path(w.name)).st_mode & 0o777
        assert mode == 0o644, f"세그먼트 권한 {oct(mode)} — 다른 사용자가 못 읽는다"
    finally:
        if hasattr(w, "close"):
            w.close()


def test_the_arm_daemons_survive_an_unreadable_action_segment():
    """열기 실패(권한)는 `ArmSegmentError` 가 아니라 `OSError` 라 명령 스레드가 **조용히
    죽었다** — 저널엔 트레이스백 한 줄, 팔은 영영 정지. simd 와 robotd 둘 다 한 번 말하고
    재시도해야 한다."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for rel in ("sim/piper_sim/bridge.py", "robot/piper_robot/publish.py"):
        loop = (repo / rel).read_text().split("def _command_loop", 1)[1].split("read_new", 1)[0]
        assert "except OSError as exc:" in loop and "_open_warned" in loop, f"{rel}: 열기 실패로 스레드가 죽는다"


def test_state_roundtrip():
    w = StateWriter(IFACE)
    try:
        can_ns = time.time_ns()
        assert w.publish(POSE, err_code=0x1234, ctrl_mode=0x06, can_wall_ns=can_ns) == 1
        r = StateReader(IFACE)
        try:
            got = r.read()
            assert got["values"] == pytest.approx(POSE)
            assert got["err_code"] == 0x1234
            assert got["ctrl_mode"] == 0x06
            assert got["can_wall_ns"] == can_ns
            assert got["seq"] == 1
            assert r.retries == 0
        finally:
            r.close()
    finally:
        w.close()


def test_action_roundtrip():
    w = ActionWriter(IFACE, deadman_ms=200)
    try:
        w.publish(POSE)
        r = ActionReader(IFACE)
        try:
            got = r.read()
            assert got["values"] == pytest.approx(POSE)
            assert got["issued_wall_ns"] > 0
            assert r.deadman_ms == 200
        finally:
            r.close()
    finally:
        w.close()


def test_missing_joint_is_loud():
    """빠진 관절을 0으로 채우지 않는다.

    0은 정규화 좌표에서 "가운데"라 그럴듯해 보인다. 그게 명령으로 되돌아오면
    팔이 **움직인다** — 조용히 넘어가면 안 되는 종류의 실수다.
    """
    w = StateWriter(IFACE)
    try:
        with pytest.raises(ValueError, match="joint6"):
            w.publish({j: 0.0 for j in JOINTS if j != "joint6"})
    finally:
        w.close()


def test_direction_mismatch_is_rejected():
    """이름이 방향을 담으므로(`can0.state` / `can0.action`) 보통은 섞일 수 없다.
    `kind` 검사는 그 이름 규칙이 깨졌을 때를 위한 **두 번째 방어선**이다.

    두 레코드는 크기가 달라서, 섞이면 엉뚱한 오프셋에서 float 를 읽는다 —
    예외 없이 그럴듯한 쓰레기 자세가 나오는 종류의 실패다.
    """
    # action 이름의 파일에 state 내용을 쓴다 (이름 규칙이 깨진 상황)
    src = StateWriter(IFACE)
    try:
        src.publish(POSE)
        A.segment_path(A.segment_name(IFACE, A.KIND_ACTION)).write_bytes(
            A.segment_path(src.name).read_bytes()
        )
        with pytest.raises(ArmSegmentError, match="방향"):
            ActionReader(IFACE)
    finally:
        src.close()


def test_missing_segment_names_the_daemon():
    with pytest.raises(ArmSegmentError, match="robotd"):
        StateReader("pytest-nonexistent")


def test_bad_magic_is_rejected():
    """포맷이 안 맞으면 **즉시** 실패한다. 관절 값은 쓰레기여도 그럴듯해 보인다."""
    w = StateWriter(IFACE)
    try:
        path = A.segment_path(w.name)
        raw = bytearray(path.read_bytes())
        raw[0:4] = struct.pack("<I", 0xDEADBEEF)
        path.write_bytes(bytes(raw))
        with pytest.raises(ArmSegmentError, match="매직"):
            StateReader(IFACE)
    finally:
        w.close()


def test_deadman_needs_an_explicit_declaration():
    """`deadman_ms=0` 은 "데드맨 없음"이지 "항상 정지"가 아니다.

    판정 불가와 안전을 헷갈리면, 데드맨을 선언하지 않은 텔레오퍼레이션이
    시작하자마자 멈춘다.
    """
    w = ActionWriter(IFACE)          # 선언 안 함
    try:
        w.publish(POSE)
        r = ActionReader(IFACE)
        try:
            assert r.deadman_ms == 0
            assert not r.is_stale()
        finally:
            r.close()
    finally:
        w.close()


def test_deadman_trips_when_the_consumer_stops():
    w = ActionWriter(IFACE, deadman_ms=50)
    try:
        w.publish(POSE)
        r = ActionReader(IFACE)
        try:
            assert not r.is_stale()
            time.sleep(0.12)
            assert r.is_stale(), "소비자가 멈췄는데 데드맨이 안 걸린다"
            w.publish(POSE)          # 다시 살아나면 풀린다
            assert not r.is_stale()
        finally:
            r.close()
    finally:
        w.close()


@pytest.mark.parametrize(("writer", "reader"), [
    (StateWriter, StateReader),
    (ActionWriter, ActionReader),      # ⚠ 이쪽을 빼먹어 실기에서 터졌다
])
def test_read_new_waits_for_a_fresh_record(writer, reader):
    """**방향이 둘인 세그먼트라 양쪽 다 본다.**

    회귀: `read_new` 가 `StateReader` 에만 있어서, 명령을 소비하는 쪽이
    `AttributeError` 로 매번 재접속했다. 상태 경로만 테스트해서 못 잡았다.
    """
    w = writer(IFACE)
    try:
        w.publish(POSE)
        r = reader(IFACE)
        try:
            assert r.read()["seq"] == 1
            assert r.read_new(timeout_s=0.05) is None, "새 것이 없는데 옛 것을 준다"
            w.publish({j: 1.0 for j in JOINTS})
            got = r.read_new(timeout_s=0.5)
            assert got is not None and got["seq"] == 2
        finally:
            r.close()
    finally:
        w.close()


def test_both_directions_expose_the_same_surface():
    """양쪽 리더가 **같은 동작 집합**을 가져야 한다.

    한쪽에만 메서드를 달면 다른 쪽 경로가 런타임에 터진다 — 그것도 팔이
    움직이는 중에. 방향만 다를 뿐 하는 일은 같으니 표면도 같아야 한다.
    """
    def surface(cls):
        return {n for n in dir(cls) if not n.startswith("_")}

    assert surface(StateReader) == surface(ActionReader) - {"is_stale"}
    assert surface(StateWriter) == surface(ActionWriter)


def test_segment_existence_is_the_lease():
    """세그먼트가 있다는 것 자체가 "누가 이 CAN 을 쥐고 있다"는 뜻이다."""
    assert A.segment_name(IFACE, A.KIND_STATE) not in A.list_segments()
    w = StateWriter(IFACE)
    try:
        assert A.segment_name(IFACE, A.KIND_STATE) in A.list_segments()
    finally:
        w.close()
    assert A.segment_name(IFACE, A.KIND_STATE) not in A.list_segments(), "누수"
