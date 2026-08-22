"""깊이로 컬러의 배경을 지운다 (feature/depth-background-mask.md).

정책이 배경을 안 보게 하는 것이 목적이다 — 작업대를 옮기거나 뒤로 사람이
지나가도 같은 관측이 되게.

여기서 잠그는 것은 **틀렸을 때 조용한 것들**이다: 무효 픽셀 처리, 정렬,
그리고 "지워진 데이터인지 나중에 알 수 있는가".
"""

import numpy as np
import pytest

pytest.importorskip("piper_rs")
from piper_rs import mask as M  # noqa: E402

UNITS_D405 = 0.0001   # raw 1 = 0.1mm
UNITS_D435 = 0.001    # raw 1 = 1mm


def _depth(mm_values, units_m):
    """mm 목록을 그 장치의 raw 로 바꾼 (1, N) 깊이 프레임."""
    raw = [0 if v is None else int(round(v / (units_m * 1000.0))) for v in mm_values]
    return np.array([raw], np.uint16)


def _color(n, value=200):
    return np.full((1, n, 3), value, np.uint8)


def test_pixels_beyond_the_boundary_are_erased():
    d = _depth([100, 400, 900], UNITS_D435)
    out = M.apply_mask(_color(3), d, far_mm=500, units_m=UNITS_D435)
    assert list(out[0, :, 0]) == [200, 200, 0]


def test_unknown_depth_is_kept_not_erased():
    """⚠ **깊이가 없다고 물체가 없는 게 아니다.**

    D405 는 실측에서 프레임의 42% 를 못 읽었다(범위 밖·무늬 없는 면·반사).
    그걸 배경으로 치면 물체 한가운데가 숭숭 뚫린다 — 배경이 덜 잘리는 것보다
    나쁜 결과다.
    """
    d = _depth([None, None, 900], UNITS_D435)
    out = M.apply_mask(_color(3), d, far_mm=500, units_m=UNITS_D435)
    assert list(out[0, :, 0]) == [200, 200, 0], "모르는 픽셀을 배경으로 쳤다"


def test_nothing_closer_than_the_boundary_is_erased():
    """**near 는 안 자른다.** 배경은 뒤에 있는 것이다.

    카메라에 아주 가까운 것을 지우면 집으려는 물체가 손 앞에 왔을 때 사라진다 —
    정확히 필요한 순간에 안 보이게 된다.
    """
    d = _depth([1, 5, 20, 60], UNITS_D435)      # 전부 far 보다 훨씬 가깝다
    out = M.apply_mask(_color(4), d, far_mm=500, units_m=UNITS_D435)
    assert (out == 200).all(), "가까운 것을 지웠다"


def test_the_boundary_means_the_same_distance_on_every_device():
    """`far_mm` 은 실제 거리다 — D405(0.1mm 단위)에서도 같은 곳을 가른다.

    `depth_units` 를 안 보면 D405 에서 10배 어긋난다 (`encode_depth` 와 같은 함정).
    """
    for mm, keep in ((400, True), (600, False)):
        a = M.apply_mask(_color(1), _depth([mm], UNITS_D435), 500, UNITS_D435)
        b = M.apply_mask(_color(1), _depth([mm], UNITS_D405), 500, UNITS_D405)
        assert bool(a[0, 0, 0] == 200) is keep
        assert a[0, 0, 0] == b[0, 0, 0], f"{mm}mm 가 장치마다 다르게 판정된다"


def test_a_misaligned_depth_frame_masks_nothing():
    """어긋난 마스크는 안 하느니만 못하다 — 물체를 엉뚱한 데서 지운다.

    깊이와 컬러는 다른 센서라 해상도도 다를 수 있다(D435 컬러 640x480 / 깊이 848x480).
    """
    color = np.full((480, 640, 3), 200, np.uint8)
    depth = np.zeros((480, 848), np.uint16)
    assert not M.shapes_match(color, depth)
    assert (M.apply_mask(color, depth, 500, UNITS_D435) == 200).all()


def test_bad_parameters_are_rejected_not_divided_by():
    for far, units in ((0, UNITS_D435), (-1, UNITS_D435), (500, 0), (500, -1)):
        with pytest.raises(ValueError):
            M.background_mask(np.zeros((1, 1), np.uint16), far, units)


def test_the_original_is_not_modified_in_place():
    """`_latest` 와 발행 버퍼가 같은 배열을 볼 수 있다 — 제자리에서 고치면
    마스킹이 안 켜진 소비자까지 지워진 프레임을 본다."""
    c = _color(2)
    M.apply_mask(c, _depth([900, 900], UNITS_D435), 500, UNITS_D435)
    assert (c == 200).all()


# ── 데몬 배선 ────────────────────────────────────────────────────────────────

def test_alignment_only_runs_when_masking_is_on():
    """`rs.align` 은 프레임마다 재투영을 돌린다 — 안 쓰는 실행에 물리면 안 된다."""
    import inspect

    from piper_rs.hub import _RSDevice

    src = inspect.getsource(_RSDevice._aligned_depth)
    assert "self.mask_background" in src, "켜짐 여부를 안 본다"
    assert "rs.align" in src


def test_the_published_depth_stays_unaligned():
    """정렬하면 시점과 해상도가 컬러 쪽으로 바뀐다.

    발행하는 깊이까지 바꾸면 **예전 데이터셋과 뜻이 달라진다** — 마스킹을 켠 것뿐인데
    깊이 스트림의 의미가 조용히 이동한다.
    """
    import inspect

    from piper_rs.hub import _RSDevice

    src = inspect.getsource(_RSDevice._read_loop)
    depth_block = src.split('if "depth" in self._active', 1)[1].split('if "infrared"', 1)[0]
    assert "aligned_depth" not in depth_block, "정렬한 깊이를 발행한다"
    assert "frames.get_depth_frame()" in depth_block


def test_whether_the_background_was_erased_is_recorded():
    """⚠ **프레임만 봐서는 못 가린다** — 어두운 배경과 지워진 배경이 똑같이 검다.

    사이드카에 안 남기면 나중에 그 데이터가 어떻게 찍혔는지 영영 모른다.
    """
    import inspect

    from app.services import camera_config

    src = inspect.getsource(camera_config)
    assert 'entry["background_mask"]' in src, "사이드카에 안 남는다"


def test_changing_the_mask_is_blocked_while_recording():
    """도중에 바꾸면 한 데이터셋에 배경이 있는 프레임과 없는 프레임이 섞인다.
    사이드카에는 정지 시점의 값 하나만 남아 거짓이 된다."""
    import inspect

    from app.routers import cameras

    src = inspect.getsource(cameras.set_background_mask)
    assert "require_idle" in src, "녹화 중에도 바꿀 수 있다"
