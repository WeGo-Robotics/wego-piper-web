"""깊이맵 인코딩 — 정책 입력 계약 (refactor/camera-transport.md).

**전부 하드웨어 없이 돈다.** 인코딩은 순수 함수다 — 안전 필터와 같은 이유로,
경계 조건을 실제 카메라로 확인하는 건 느리고 재현이 안 된다.
"""

import numpy as np
import pytest

pytest.importorskip("piper_rs")
from piper_rs.depth import INVALID, VALID_MAX, DepthEncoding, encode_depth  # noqa: E402

ENC = DepthEncoding(near_mm=150, far_mm=1200)


def _d(*vals) -> np.ndarray:
    return np.array([list(vals)], dtype=np.uint16)


def test_output_is_three_channel_uint8():
    """LeRobot 데이터셋 계층은 미터법 depth 를 저장할 수 없다 — 어차피 변환된다."""
    out = encode_depth(np.zeros((4, 6), dtype=np.uint16), ENC)
    assert out.shape == (4, 6, 3) and out.dtype == np.uint8


def test_encoding_is_monotonic():
    """**거리와 픽셀값이 1:1 로 올라야 한다.**

    JET 컬러맵이 안 되는 이유가 이것이다 — 파랑→초록→빨강이 채널별로 오르내려서,
    컨볼루션이 "더 밝다 = 더 멀다"를 배울 수 없다.
    """
    d = _d(200, 400, 600, 800, 1000)
    v = encode_depth(d, ENC)[0, :, 0].astype(int)
    assert all(a < b for a, b in zip(v, v[1:])), f"단조롭지 않다: {v}"


def test_invalid_pixels_read_as_farthest_not_nearest():
    """**RealSense 는 못 읽은 픽셀을 0 으로 준다.**

    그게 0m 로 해석되면 눈앞에 벽이 있는 것처럼 학습된다. 없는 것은 '가장 멂'으로
    두는 편이 안전한 쪽으로 틀린다.
    """
    out = encode_depth(_d(0, 150, 1200), ENC)[0, :, 0]
    assert out[0] == INVALID
    assert out[0] > out[2], "무효 픽셀이 가장 먼 유효값보다 어둡다 = 가깝게 보인다"
    assert out[1] == 0, "near 는 0 이어야 한다 (가까울수록 어둡다)"


def test_valid_range_never_collides_with_invalid():
    """유효값이 255 를 쓰면 무효와 구별할 수 없다."""
    d = np.arange(0, 3000, 7, dtype=np.uint16).reshape(1, -1)
    v = encode_depth(d, ENC)[0, :, 0]
    valid = v[d[0] != 0]
    assert valid.max() <= VALID_MAX, "유효 구간이 무효값(255)까지 침범한다"


def test_out_of_range_is_clipped_not_wrapped():
    """자르지 않으면 uint8 로 접히면서 **먼 것이 가까워 보인다.**"""
    out = encode_depth(_d(10, 60000), ENC)[0, :, 0]
    assert out[0] == 0, "near 아래가 0 으로 안 잘렸다"
    assert out[1] == VALID_MAX, "far 위가 최대로 안 잘렸다"


def test_narrow_range_uses_the_full_scale():
    """작업 공간을 좁히면 해상도가 올라가야 한다 — 하드코딩 alpha 가 못 하던 것이다."""
    tight = DepthEncoding(near_mm=300, far_mm=500)
    v = encode_depth(_d(300, 400, 500), tight)[0, :, 0].astype(int)
    assert v[0] == 0 and v[2] == VALID_MAX
    assert 120 < v[1] < 135, f"중간값이 가운데가 아니다: {v[1]}"


def test_bad_range_is_rejected():
    """far <= near 면 0 으로 나누거나 뒤집힌 인코딩이 나온다 — 조용히 넘기지 않는다."""
    with pytest.raises(ValueError, match="far_mm"):
        encode_depth(_d(100), DepthEncoding(near_mm=500, far_mm=500))


def test_params_travel_with_the_stream():
    """같은 픽셀값이 실행마다 다른 거리를 뜻하면 데이터셋이 조용히 오염된다.

    rsd 가 파라미터를 소유하고 `info()` 로 내보내야 메타에 남길 수 있다.
    """
    import ast
    import importlib.util
    from pathlib import Path

    src = Path(importlib.util.find_spec("piper_rs.hub").origin).read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "info")
    keys = {n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "depth_encoding" in keys, "info() 가 인코딩 파라미터를 안 내보낸다"
    assert set(DepthEncoding().to_dict()) == {"near_mm", "far_mm", "mode"}


# ── 데이터셋 사이드카 (LeRobot 은 카메라 설정을 안 적는다) ──

def test_sidecar_carries_what_the_frames_cannot_tell():
    """해상도는 비디오에 있지만 **깊이 인코딩 범위는 어디에도 없다.**

    없으면 나중에 클리핑을 바꿔 녹화한 데이터와 섞였을 때 구별할 방법이 없다 —
    에러 없이 정책만 나빠지는 종류의 오염이다.
    """
    from app.services import camera_config as cc
    from app.services.camera_manager import camera_manager

    class _Cam:
        def running_profile(self):
            return {"width": 848, "height": 480, "fps": 30,
                    "depth_encoding": {"near_mm": 300, "far_mm": 700, "mode": "gray_linear"}}

    cam_id = "rs:pytest:depth"
    camera_manager.cameras[cam_id] = _Cam()
    try:
        out = cc.camera_sidecar({"d": cam_id})["cameras"]["d"]
        assert out["depth_encoding"]["near_mm"] == 300
        assert out["id"] == cam_id and out["fps"] == 30
    finally:
        camera_manager.cameras.pop(cam_id, None)


def test_recording_writes_the_sidecar_on_stop():
    """사이드카는 데이터셋이 **만들어진 뒤**에야 쓸 수 있다 — 시작 때는 못 남긴다.

    그래서 시작 시점의 매핑을 붙잡아 뒀다가 정지 후에 쓴다.
    """
    import ast
    import inspect

    from app.routers import recording

    stop = inspect.getsource(recording.stop_recording)
    calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(stop.lstrip()))
             if isinstance(n, ast.Call)}
    assert "write_camera_sidecar" in calls, "정지 때 사이드카를 안 쓴다"

    start = inspect.getsource(recording.start_recording)
    assert "_last_recording" in start, "시작 때 매핑을 안 붙잡는다"


def test_changing_the_range_is_blocked_while_recording():
    """녹화 중에 바꾸면 **한 데이터셋 안에서 픽셀값의 뜻이 달라진다.**

    사이드카에는 정지 시점의 값 하나만 남아 거짓이 된다.
    """
    import inspect

    from app.routers import cameras

    src = inspect.getsource(cameras.set_depth_encoding)
    assert "require_idle" in src, "녹화 중 변경을 안 막는다"
