"""shm 세그먼트의 색 순서는 **BGR** 이다.

노란 물체가 파랗게 나왔다. 세그먼트→JPEG 코드가 세 군데에 복사돼 있었는데
그중 `shm_snapshot` 하나만 `frame[:, :, ::-1]` 로 채널을 뒤집었다 — "세그먼트는
RGB" 라는 주석과 함께. 아니었다. 그래서 그 함수를 쓰는 화면(비전 페이지,
YOLO 캡처)에서만 색이 뒤집혀 보였고, 카메라 페이지 프리뷰는 멀쩡했다.

⚠ 화면만의 문제가 아니었다 — YOLO 학습 이미지가 그 경로로 저장된다.
"""

import re
from pathlib import Path

import numpy as np
import pytest

_SVC = Path(__file__).resolve().parents[1] / "app" / "services"

# 노란색: BGR 로 [0, 255, 255]. R 과 B 가 바뀌면 [255, 255, 0] = 파랑 쪽이 된다.
YELLOW_BGR = (0, 255, 255)


def test_a_bgr_frame_encodes_to_a_yellow_jpeg(tmp_path, monkeypatch):
    """⚠ **회귀** — 진짜 픽셀로 왕복시킨다. 소스만 읽는 검사는 다음번 변형을 놓친다."""
    cv2 = pytest.importorskip("cv2")
    from app.services import shm_snapshot

    frame = np.full((16, 16, 3), YELLOW_BGR, dtype=np.uint8)

    class FakeSub:
        def read(self): return (frame, 0)
        def close(self): pass

    import sys, types
    fake = types.ModuleType("piper_shm")
    fake.SegmentError = RuntimeError
    fake.Subscriber = lambda name: FakeSub()
    fake.segment_for_camera = lambda name: name
    monkeypatch.setitem(sys.modules, "piper_shm", fake)

    data = shm_snapshot.segment_jpeg("cam0")
    assert data, "인코딩이 안 됐다"
    back = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    b, g, r = (int(v) for v in back[8, 8])
    assert b < 60 and g > 200 and r > 200, \
        f"노란색이 아니다 (B={b} G={g} R={r}) — 채널이 뒤집혔다"


def test_there_is_one_segment_to_jpeg_implementation():
    """세 벌이었고, 한 벌만 색 순서를 다르게 알고 있었다.

    같은 세그먼트를 읽는 코드가 서로 다른 규약을 갖는 것이 이 버그였다.
    """
    for name in ("realsense_manager.py", "v4l2_client.py"):
        src = (_SVC / name).read_text()
        assert "segment_jpeg" in src, f"{name} 이 공용 인코더를 안 쓴다"
        assert "IMWRITE_JPEG_QUALITY" not in src, f"{name} 이 자체 인코더를 다시 들었다"


@pytest.mark.parametrize("name", ["shm_snapshot.py", "realsense_manager.py", "v4l2_client.py"])
def test_nobody_flips_the_channels_on_the_way_out(name):
    """`[:, :, ::-1]` 한 줄이 이 버그였다. 되돌아오면 바로 걸린다."""
    from conftest import code_only

    src = code_only((_SVC / name).read_text())
    assert not re.search(r"\[:,\s*:,\s*::-1\]", src), f"{name} 이 채널을 뒤집는다"
