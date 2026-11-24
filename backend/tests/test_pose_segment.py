"""자세 shm 세그먼트 — `piper.pose.<id>` 포맷 계약 (feature/pika.md §4).

팔 세그먼트와 같은 함정을 본다: 이름표 없이 값만 담는 레코드라 포맷이 어긋나면 에러 대신 그럴듯한 쓰레기
자세가 나온다. 그리고 만드는 쪽(확장 컨테이너, root)과 읽는 쪽(호스트 사용자)이 다르다.
"""

import math
import os
import struct

import pytest

pytest.importorskip("piper_shm")
from piper_shm import PoseReader, PoseSegmentError, PoseWriter  # noqa: E402
from piper_shm import arm as A  # noqa: E402
from piper_shm import pose as P  # noqa: E402

PID = "pytest-pose"


@pytest.fixture(autouse=True)
def _clean():
    yield
    P.unlink(PID)


def test_a_pose_round_trips_with_its_flags_and_trigger_count():
    w = PoseWriter(PID)
    r = PoseReader(PID)
    try:
        assert r.read() is None, "쓰기 전에는 '아직 없음' 이어야 한다 — 0 자세를 내면 원점으로 끌려간다"
        w.publish((0.1, -0.2, 0.3), (0.0, 0.0, 0.7071, 0.7071), opening_mm=42.5,
                  tracking_ok=True, encoder_ok=True, trigger_seq=3, wall_ns=123)
        got = r.read()
        assert got["pos"] == pytest.approx((0.1, -0.2, 0.3))
        assert got["quat"] == pytest.approx((0.0, 0.0, 0.7071, 0.7071))
        assert got["opening_mm"] == pytest.approx(42.5)
        assert got["tracking_ok"] and got["encoder_ok"]
        assert got["trigger_seq"] == 3 and got["wall_ns"] == 123 and got["seq"] == 1
    finally:
        r.close()
        w.close()


def test_lost_tracking_and_a_missing_encoder_are_said_not_guessed():
    w = PoseWriter(PID)
    r = PoseReader(PID)
    try:
        w.publish((0, 0, 0), (0, 0, 0, 1), tracking_ok=False)
        got = r.read()
        assert got["tracking_ok"] is False and got["encoder_ok"] is False
        assert got["opening_mm"] is None, "벌림이 없는 소스는 None — 0mm 는 '닫힘' 이라는 값이다"
    finally:
        r.close()
        w.close()


def test_the_reader_always_sees_the_newest_pose_across_slot_wraps():
    w = PoseWriter(PID)
    r = PoseReader(PID)
    try:
        for i in range(1, 50):
            w.publish((i, 0, 0), (0, 0, 0, 1), trigger_seq=i)
        got = r.read()
        assert got["pos"][0] == 49 and got["trigger_seq"] == 49 and got["seq"] == 49
    finally:
        r.close()
        w.close()


def test_a_writer_makes_the_segment_readable_by_other_users():
    old = os.umask(0o077)
    try:
        w = PoseWriter(PID)
    finally:
        os.umask(old)
    try:
        assert os.stat(P.segment_path(PID)).st_mode & 0o777 == 0o644
    finally:
        w.close()


def test_a_restarted_source_is_noticed_by_an_open_reader():
    """라이터가 재시작하면 옛 mmap 은 마지막 자세를 영원히 낸다 — `orphaned` 로 알아채야 한다."""
    w = PoseWriter(PID)
    r = PoseReader(PID)
    try:
        assert not r.orphaned
        w.close()                       # 세그먼트를 지운다 (소스 종료)
        assert r.orphaned
    finally:
        r.close()


def test_an_arm_segment_is_refused_as_a_pose():
    """다른 레코드를 자세로 읽으면 그럴듯한 쓰레기가 나온다 — 매직에서 멈춰야 한다."""
    path = P.segment_path(PID)
    with open(path, "wb") as f:
        f.write(struct.pack(A._HEADER_FMT, A.MAGIC, A.VERSION, A.KIND_STATE, 3, A.STATE_BYTES, 0, 0, 0)
                .ljust(A.HEADER_SIZE + A.STATE_BYTES * 3, b"\0"))
    with pytest.raises(PoseSegmentError, match="매직"):
        PoseReader(PID)


@pytest.mark.parametrize("bad", ["", "../etc", "a/b", ".hidden", "x" * 65])
def test_an_id_cannot_point_outside_dev_shm(bad):
    with pytest.raises(PoseSegmentError):
        P.segment_path(bad)


def test_a_missing_source_says_so():
    with pytest.raises(PoseSegmentError, match="없습니다"):
        PoseReader("pytest-nobody")


def test_the_record_is_fixed_size_and_aligned():
    """레코드 크기는 계약이다 — 바꾸면 VERSION 을 올려야 한다."""
    assert P.REC_BYTES == 80 and P.REC_BYTES % 8 == 0
    assert math.isnan(struct.unpack("<f", struct.pack("<f", math.nan))[0])
