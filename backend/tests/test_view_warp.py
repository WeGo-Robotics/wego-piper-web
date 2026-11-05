"""정책 카메라 화면 보정 (wrapper/view_warp.py, feature/target-conditioning.md §5).

카메라가 학습 녹화 때 자리에서 비켜나면 정책은 처음 보는 화면을 받는다. 재는 쪽(apple-sort)이 버스 `view_warp`
로 "지금 화면 → 학습 화면" 변환을 보내고 wrapper 가 정책에 넣을 이미지를 편다. 그래서:

1. 버스 값 — 게이트웨이와 wrapper 가 같은 규칙으로 받고 버린다 (틀린 행렬로 펴느니 원본이 낫다)
2. 펴기 — 행렬이 말한 자리로 옮기고, 화면 밖 띠는 가장자리로 채우고, 크기가 달라도 같은 자리로 간다
3. 자리 — 그 스트림을 읽는 카메라에만, 키를 만든 직후·이름 바꾸기 전에 건다. 기록은 원본이다
"""

import sys
from pathlib import Path

import numpy as np
import pytest

from conftest import code_only

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "wrapper"))
import view_warp as W  # noqa: E402

from app.services import param_bridge as B  # noqa: E402
from app.services.param_bridge import ParamBridge, view_warp_value  # noqa: E402

# 117 위 카메라 (11-02) — 표지가 학습 때보다 (+1, +45)px 아래, ×1.019
M117 = [[0.9813, -0.0004, 5.92], [0.0004, 0.9813, -36.85]]


def _warp(m, stream="top", frame=(640, 480)):
    return {"stream": stream, "frame": list(frame), "m": m}


# ── 1. 버스 값 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("m", [
    [[1, 0, 0], [0, 1, 0]],
    M117,
    [[1.1 * np.cos(0.1), -1.1 * np.sin(0.1), 20], [1.1 * np.sin(0.1), 1.1 * np.cos(0.1), -30]],
])
def test_gateway_and_wrapper_take_the_same_warps(m):
    got = view_warp_value(_warp(m))
    assert got["stream"] == "top" and got["frame"] == [640, 480]
    assert np.allclose(got["m"], m)
    w = W.parse_view_warp(got)
    assert w.stream == "top" and w.frame == (640, 480) and np.allclose(w.m, m)


@pytest.mark.parametrize("off", [None, {}, ""])
def test_an_empty_warp_turns_it_off(off):
    assert view_warp_value(off) is None
    assert W.parse_view_warp(off) is None
    safe, _ = ParamBridge(bus=None).validate_params({"view_warp": off})
    assert "view_warp" in safe and safe["view_warp"] is None, "끄는 값이 막히면 펴진 채 남는다"


@pytest.mark.parametrize("bad", [
    [[1, 0, 0], [0, 1, 0]],                                     # dict 가 아니다
    {"frame": [640, 480], "m": [[1, 0, 0], [0, 1, 0]]},         # 스트림 없음
    _warp([[1, 0, 0], [0, 1, 0]], stream=""),
    _warp([[1, 0, 0], [0, 1, 0]], frame=(640,)),
    _warp([[1, 0, 0], [0, 1, 0]], frame=(0, 480)),
    _warp([[1, 0, 0]]),
    _warp([[1, 0], [0, 1]]),
    _warp([[float("nan"), 0, 0], [0, 1, 0]]),
    _warp([[-1, 0, 640], [0, 1, 0]]),                           # 뒤집힘
    _warp([[1.5, 0, 0], [0, 1.5, 0]]),                          # 배율
    _warp([[np.cos(0.4), -np.sin(0.4), 0], [np.sin(0.4), np.cos(0.4), 0]]),  # 회전 23°
    _warp([[1, 0, 400], [0, 1, 0]]),                            # 이동이 폭의 절반 넘게
])
def test_malformed_warps_are_dropped(bad):
    with pytest.raises((ValueError, TypeError)):
        view_warp_value(bad)
    with pytest.raises((ValueError, TypeError)):
        W.parse_view_warp(bad)
    safe, _ = ParamBridge(bus=None).validate_params({"view_warp": bad, "task": "x"})
    assert "view_warp" not in safe and safe["task"] == "x"


def test_both_sides_use_the_same_limits():
    assert B.VIEW_SCALE_RANGE == W.SCALE_RANGE
    assert B.VIEW_MAX_ROT_DEG == W.MAX_ROT_DEG
    assert B.VIEW_MAX_SHIFT_FRAC == W.MAX_SHIFT_FRAC
    assert B.VIEW_MAX_FRAME == W.MAX_FRAME


def test_a_bad_value_on_the_bus_turns_correction_off():
    slot = W.WarpSlot()
    slot.update(_warp(M117))
    with pytest.raises(ValueError):
        slot.update(_warp([[1.5, 0, 0], [0, 1.5, 0]]))
    assert slot.current is None, "틀린 값을 받고도 예전 행렬로 계속 펴면 안 된다"


# ── 2. 펴기 ─────────────────────────────────────────────────────────────

def _dot(w=640, h=480, at=(300, 400)):
    img = np.zeros((h, w, 3), np.uint8)
    img[:, :] = (40, 40, 40)
    x, y = at
    img[y - 2:y + 3, x - 2:x + 3] = 255
    return img


def _find(img):
    ys, xs = np.nonzero(img[:, :, 0] > 128)
    return xs.mean(), ys.mean()


def test_a_point_lands_where_the_matrix_says():
    pytest.importorskip("cv2")
    w = W.parse_view_warp(_warp(M117))
    out = w.apply(_dot(at=(300, 400)))
    want = np.asarray(M117) @ [300, 400, 1]
    assert np.allclose(_find(out), want, atol=0.6)


def test_the_band_from_outside_the_frame_is_filled_from_the_edge():
    pytest.importorskip("cv2")
    w = W.parse_view_warp(_warp(M117))              # 위로 37px 끌어올린다 → 아래 띠는 화면 밖에서 온다
    out = w.apply(_dot())
    assert (out[-30:] == 40).all(), "검정 띠는 학습 화면에 없던 것이다"


def test_another_frame_size_moves_to_the_same_place():
    pytest.importorskip("cv2")
    w = W.parse_view_warp(_warp(M117))
    big = _find(w.apply(_dot(at=(300, 400))))
    small = _find(w.apply(_dot(w=320, h=240, at=(150, 200))))
    assert np.allclose(np.asarray(small) * 2, big, atol=1.5)


def test_the_summary_reads_like_the_plugin_log():
    w = W.parse_view_warp(_warp(M117))
    s = w.summary()
    assert s.startswith("top: 화면 가운데가 학습 때보다 (") and "×1.019" in s


# ── 3. 자리 ─────────────────────────────────────────────────────────────

def test_only_cameras_reading_that_stream_are_warped():
    pytest.importorskip("cv2")
    slot = W.WarpSlot()
    cams = {"left_top": {"type": "shm", "segment": "top"}, "left_hand": {"type": "shm", "segment": "left"},
            "usb": {"type": "opencv", "index_or_path": 0}}
    P = "observation.images."
    obs = {P + "left_top": _dot(), P + "left_hand": _dot(), P + "usb": _dot(), "observation.state": np.zeros(7)}
    assert slot.apply(dict(obs), cams, P) == ([], None), "꺼져 있으면 아무것도 안 한다"
    slot.update(_warp(M117))
    o = dict(obs)
    keys, fresh = slot.apply(o, cams, P)
    assert keys == [P + "left_top"] and fresh is slot.current
    assert not np.array_equal(o[P + "left_top"], obs[P + "left_top"])
    assert o[P + "left_hand"] is obs[P + "left_hand"] and o[P + "usb"] is obs[P + "usb"]
    assert slot.apply(dict(obs), cams, P)[1] is None, "알리는 것은 처음 한 번"


def test_the_wrapper_warps_right_after_building_the_keys():
    src = code_only((_REPO / "wrapper" / "lerobot_wrapper.py").read_text())
    build = src.index('lerobot_obs = build_dataset_frame(lerobot_features, raw_obs, prefix="observation")')
    warp = src.index("_view_warp.apply(lerobot_obs, cameras_cfg, OBS_IMAGES)")
    assert build < warp < src.index("if _rename_map:", build), "이름을 바꾼 뒤에는 카메라 이름으로 못 찾는다"
    assert 'if key == "view_warp":' in src and "_view_warp.update(value)" in src
