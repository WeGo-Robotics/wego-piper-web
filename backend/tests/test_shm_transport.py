"""`/dev/shm` 프레임 전송 (refactor/camera-transport.md 착수 순서 1~2단계).

**데몬을 쪼개기 전에 전송이 되는지부터** 확인하는 단계다. 지금은 게이트웨이의
`camera_manager` 가 임시로 세그먼트를 채우고, 소비자(LeRobot 플러그인)가 읽는다.

여기서 잠그는 것:

1. **찢어진 프레임이 정책에 들어가지 않는다** — seqlock 이 전부다
2. **세그먼트가 남지 않는다** — 남으면 소비자가 "멈춘 화면"을 본다
3. 소비자가 라이터 버퍼를 그대로 보지 않는다 (복사)
"""

import numpy as np
import pytest

pytest.importorskip("piper_shm")
from piper_shm import (  # noqa: E402
    Layout,
    Publisher,
    SegmentError,
    Subscriber,
    list_segments,
    segment_path,
    unlink,
)
from piper_shm import segment as S  # noqa: E402

_NAME = "pytest_shm"


@pytest.fixture(autouse=True)
def clean():
    unlink(_NAME)
    yield
    unlink(_NAME)


def _frame(v: int, h=48, w=64) -> np.ndarray:
    return np.full((h, w, 3), v, np.uint8)


def test_roundtrip_preserves_pixels_exactly():
    """**JPEG 이중압축이 없다**는 것이 이 전송을 고른 이유 중 하나다."""
    pub = Publisher(_NAME, 64, 48)
    sub = Subscriber(_NAME)
    src = np.random.randint(0, 255, (48, 64, 3), dtype=np.uint8)
    pub.publish(src)
    got, seq, wall = sub.read()
    assert np.array_equal(got, src), "픽셀이 변했다"
    assert seq == 1 and wall > 0
    sub.close(); pub.close()


def test_reader_gets_a_copy_not_the_live_slot():
    """복사가 없으면 라이터가 덮어써 **정책이 반쯤 갈린 프레임을 본다.**"""
    pub = Publisher(_NAME, 64, 48)
    sub = Subscriber(_NAME)
    pub.publish(_frame(1))
    got, _, _ = sub.read()
    for v in range(2, 6):
        pub.publish(_frame(v))
    assert got[0, 0, 0] == 1, "리더 버퍼가 라이터에 덮였다"
    sub.close(); pub.close()


def test_no_frame_yet_returns_none():
    pub = Publisher(_NAME, 64, 48)
    sub = Subscriber(_NAME)
    assert sub.read() is None
    assert sub.read_new(timeout_s=0.05) is None
    sub.close(); pub.close()


def test_seqlock_never_yields_a_torn_frame():
    """**핵심.** 라이터를 마구 돌리는 동안 읽어도 한 프레임 안의 값이 섞이면 안 된다.

    각 프레임을 단색으로 발행하므로, 찢어졌다면 한 배열에 두 값이 섞여 나온다.
    """
    pub = Publisher(_NAME, 128, 96)
    sub = Subscriber(_NAME)
    torn = 0
    for v in range(1, 400):
        pub.publish(_frame(v % 251 + 1, 96, 128))
        got = sub.read()
        if got is None:
            continue
        if len(np.unique(got[0])) != 1:
            torn += 1
    assert torn == 0, f"찢어진 프레임 {torn}개"
    sub.close(); pub.close()


def test_reader_retries_when_writer_laps_it():
    """라이터가 한 바퀴 돌면 재시도한다 — 조용히 옛 프레임을 주지 않는다."""
    pub = Publisher(_NAME, 32, 32, n_slots=3)
    sub = Subscriber(_NAME, max_retries=2)
    for v in range(1, 10):
        pub.publish(_frame(v, 32, 32))
    got = sub.read()
    assert got is not None and got[1] == 9, "최신 프레임이 아니다"
    sub.close(); pub.close()


def test_shape_mismatch_is_loud():
    """조용히 리사이즈하면 **정책 입력 크기가 말없이 바뀐다.**"""
    pub = Publisher(_NAME, 64, 48)
    with pytest.raises(ValueError, match="프레임 모양"):
        pub.publish(_frame(1, 10, 10))
    pub.close()


def test_close_unlinks_the_segment():
    """⚠ 남기면 소비자가 그 세그먼트를 열고 **멈춘 화면**을 본다."""
    pub = Publisher(_NAME, 32, 32)
    assert _NAME in list_segments()
    pub.close()
    assert _NAME not in list_segments()
    assert not segment_path(_NAME).exists()


def test_missing_segment_fails_cleanly():
    """발행자가 없으면 소비자는 **깨끗하게** 죽어야 한다 — 좀비로 남지 않는다."""
    with pytest.raises(SegmentError, match="세그먼트가 없습니다"):
        Subscriber("definitely_not_there")


def test_bad_magic_is_rejected():
    """엉뚱한 파일을 세그먼트로 읽으면 쓰레기 프레임이 정책에 들어간다."""
    path = segment_path(_NAME)
    path.write_bytes(b"\0" * 4096)
    with pytest.raises(SegmentError, match="매직|크기"):
        Subscriber(_NAME)


def test_layout_math():
    lay = Layout(width=640, height=480, channels=3)
    assert lay.slot_bytes == 640 * 480 * 3
    assert lay.total_bytes == S.HEADER_SIZE + lay.slot_bytes * lay.n_slots


def test_header_fits_one_cache_line():
    """슬롯 시작이 캐시라인에 정렬돼야 memcpy 가 빠르다."""
    import struct

    assert struct.calcsize(S._HEADER_FMT) <= S.HEADER_SIZE == 64


# ── 발행측(게이트웨이 임시 배선) ────────────────────────────────────────────

def test_publisher_recreates_on_resolution_change():
    """해상도가 바뀌었는데 옛 세그먼트를 두면 소비자가 옛 크기로 읽어 깨진다."""
    from app.services.shm_publisher import ShmPublisher

    pub = ShmPublisher()
    assert pub.publish(_NAME, _frame(1, 48, 64))
    assert Subscriber(_NAME).shape == (48, 64, 3)
    assert pub.publish(_NAME, _frame(2, 96, 128))
    assert Subscriber(_NAME).shape == (96, 128, 3)
    pub.stop_all()


def test_publish_never_raises_into_the_capture_loop():
    """캡처 루프에서 불린다 — 여기서 던지면 프리뷰·녹화까지 같이 멈춘다."""
    from app.services.shm_publisher import ShmPublisher

    pub = ShmPublisher()
    assert pub.publish(_NAME, None) is False          # noqa: FBT003
    assert pub.publish(_NAME, np.zeros((5, 5), np.uint8)) is False   # 2차원
    pub.stop_all()


def test_sweep_removes_orphans_but_keeps_the_living():
    """프로세스가 죽으면 세그먼트가 남는다 — 기동 시 쓸어낸다."""
    from app.services.shm_publisher import sweep_stale_segments

    # 프로세스가 죽으면 파일만 남는다 — 그 상태를 직접 만든다
    Publisher(_NAME, 32, 32).close(unlink=False)
    assert _NAME in list_segments()

    keeper = Publisher("pytest_keep", 32, 32)
    try:
        removed = sweep_stale_segments(keep={"pytest_keep"})
        assert _NAME in removed
        assert "pytest_keep" in list_segments(), "살아 있는 세그먼트를 지웠다"
    finally:
        keeper.close()


# ── 카메라 JSON 조립은 한 곳에서 ────────────────────────────────────────────

def test_recording_and_inference_share_one_builder():
    """**회귀** — 프론트가 따로 조립하면 백엔드 설정을 몰라 **녹화만 옛 경로**를 탄다.

    실제로 그랬다: 추론은 shm 으로 전환됐는데 녹화는 `RecordingPage.tsx` 가
    intelrealsense JSON 을 만들어 보내고 있었다.
    """
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    page = (repo / "frontend" / "src" / "pages" / "RecordingPage.tsx").read_text()
    assert "buildCameraConfig" not in page, "프론트가 아직 카메라 JSON 을 조립한다"
    assert "camera_mapping" in page, "매핑을 보내지 않는다"

    for router in ("models.py", "recording.py"):
        src = (repo / "backend" / "app" / "routers" / router).read_text()
        assert "build_cameras_json" in src, f"{router} 가 공용 조립기를 안 쓴다"


def test_transport_switch_changes_both_paths(monkeypatch):
    """스위치 하나로 추론·녹화가 함께 바뀌어야 한다."""
    from app.core.config import settings
    from app.services.camera_config import build_cameras_json

    mapping = {"top": "rs:1:color", "wrist": "/dev/video0"}

    monkeypatch.setattr(settings, "camera_transport", "shm")
    shm = build_cameras_json(mapping, width=640, height=480, fps=15)
    assert all(v == {"type": "shm", "segment": k} for k, v in shm.items())
    # 해상도는 발행자가 정한다 — 소비자 설정이 새어들면 안 된다
    assert not any("width" in v for v in shm.values())

    monkeypatch.setattr(settings, "camera_transport", "direct")
    direct = build_cameras_json(mapping, width=640, height=480, fps=15)
    assert direct["top"]["type"] == "intelrealsense"
    assert direct["wrist"]["type"] == "opencv"
    assert direct["top"]["width"] == 640, "direct 는 요청 해상도를 실어야 한다"


def test_prepare_is_inverted_between_transports():
    """`direct` 는 카메라를 **해제**하고 `shm` 은 **붙잡는다** — 정반대다.

    이 뒤바뀜을 놓치면 shm 에서 카메라를 해제해 프레임이 끊긴다.
    """
    import inspect

    from app.services import camera_config

    src = inspect.getsource(camera_config.prepare_cameras)
    assert "release_all_cameras" in src, "direct 경로에서 해제하지 않는다"
    assert "cam.connect()" in src, "shm 경로에서 붙잡지 않는다"
    assert 'camera_transport != "shm"' in src, "전송 방식을 안 본다"
