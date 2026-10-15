"""이미지 엔코더 프로브의 진단 토글 — norm 맵 · 평균 중심화 · 층 선택 · 패딩 유무.

2026-10-14: SmolVLA 의 SigLIP 을 사과 사진으로 돌렸더니 사과 없는 흰 배경에도 사과와 비슷한
특징이 나왔다. 후보 원인은 (1) 균일 패치가 전역 맥락을 담는다 (2) 패딩 토큰이 그 자리가 된다
(3) 공통 성분(anisotropy)이 코사인을 부풀린다 (4) high-norm 아티팩트 토큰 (5) PCA 3성분의
한계다. 이 파일은 그 **구분을 가능하게 하는 도구**가 맞게 재는지를 본다 — 모델 없이, 합성 특징으로.
"""

import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest

from app.services import encoder_probe as E

_REPO = Path(__file__).resolve().parents[2]


def _session(tmp_path, feat, *, gh=8, gw=8, row0=0, col0=0, name="s"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    np.save(d / "features.npy", feat.astype(np.float32))
    meta = {"grid_h": gh, "grid_w": gw, "valid_row0": row0, "valid_col0": col0,
            "n_patches": gh * gw, "dim": feat.shape[1]}
    (d / "meta.json").write_text(json.dumps(meta))
    return E.ProbeSession(sid=name, path=d, meta=meta)


def _unit(rng, n, dim):
    x = rng.normal(size=(n, dim))
    return x / np.linalg.norm(x, axis=1, keepdims=True)


# ── norm 맵 ─────────────────────────────────────────────────────────────

def test_norm_map_finds_the_loud_token_and_says_how_many(tmp_path):
    rng = np.random.default_rng(0)
    feat = _unit(rng, 64, 32)                      # 모든 토큰 노름 1
    feat[4 * 8 + 4] *= 5.0                         # (4,4) 하나만 5배
    feat[5 * 8 + 1] *= 2.5                         # (5,1) 은 2배는 넘고 3배는 못 넘는다
    m = E.norm_map(_session(tmp_path, feat))

    assert m["max_ratio"] == pytest.approx(5.0, rel=0.02)
    assert m["max_at"] == [4, 4]
    assert (m["over2"], m["over3"]) == (2, 1), "몇 개가 튀는지 — `최대/중앙값` 하나로는 모른다"
    assert m["n_valid"] == 64
    assert len(m["values"]) == 64
    assert m["values"][4 * 8 + 4] == pytest.approx(5.0, rel=0.02)


def test_norm_map_includes_padding_tokens_which_the_stats_leave_out(tmp_path):
    """모델은 패딩을 attention 에 넣는다. 통계(`feature_stats`)는 빼지만 이 지도는 보여준다 —
    균일한 토큰이 전역 정보를 담는 자리가 됐는지를 거기서 본다."""
    rng = np.random.default_rng(1)
    feat = _unit(rng, 64, 32)
    feat[:16] *= 3.0                               # 위 두 행(16토큰)이 패딩이고 3배 크다
    sess = _session(tmp_path, feat, row0=2)
    m = E.norm_map(sess)

    assert m["padding"]["n"] == 16
    assert m["padding"]["median_ratio"] == pytest.approx(3.0, rel=0.02)
    assert m["n_valid"] == 48
    # 유효 패치만 본 통계에는 안 잡힌다
    assert E.feature_stats(sess)["norm_outlier_ratio"] < 1.5
    # 그리고 지도에는 있다
    assert max(m["values"][:16]) == pytest.approx(3.0, rel=0.02)


def test_norm_map_without_padding_reports_none(tmp_path):
    rng = np.random.default_rng(2)
    m = E.norm_map(_session(tmp_path, _unit(rng, 64, 16)))
    assert m["padding"] == {"n": 0, "median_ratio": None, "max_ratio": None}


# ── 평균 중심화 ─────────────────────────────────────────────────────────

def _anisotropic(rng, n=64, dim=64, common=6.0, obj_patches=(18, 19, 26, 27)):
    """모든 패치가 큰 **공통 성분**을 공유하고, 물체 패치 몇 개만 고유 방향을 하나 더 가진다."""
    c = rng.normal(size=dim)
    c /= np.linalg.norm(c)
    o = rng.normal(size=dim)
    o -= (o @ c) * c
    o /= np.linalg.norm(o)
    feat = common * c + 0.5 * rng.normal(size=(n, dim)) / np.sqrt(dim)
    for i in obj_patches:
        feat[i] += 2.0 * o
    return feat, set(obj_patches)


def test_raw_cosine_says_everything_is_like_the_object_until_the_mean_is_removed(tmp_path):
    """CLIP 계열의 공통 성분이 원본 코사인을 부풀린다. 평균을 빼면 물체끼리만 남아야 한다."""
    rng = np.random.default_rng(3)
    feat, obj = _anisotropic(rng)
    sess = _session(tmp_path, feat)
    q = 18

    raw = E.cosine_map(sess, sess, q)
    cen = E.cosine_map(sess, sess, q, center=True)
    bg = [i for i in range(64) if i not in obj]

    raw_bg = float(np.mean([raw["values"][i] for i in bg]))
    cen_bg = float(np.mean([cen["values"][i] for i in bg]))
    cen_obj = float(np.mean([cen["values"][i] for i in obj if i != q]))

    assert raw_bg > 0.9, f"공통 성분이 있으면 배경도 비슷해 보인다 ({raw_bg:.2f})"
    assert cen_bg < 0.3, f"평균을 빼면 배경과의 유사도가 사라져야 한다 ({cen_bg:.2f})"
    assert cen_obj > 0.6, f"같은 물체끼리는 남아야 한다 ({cen_obj:.2f})"
    assert cen["centered"] is True and raw["centered"] is False


def test_centering_uses_the_reference_image_so_images_stay_comparable(tmp_path):
    """이미지마다 따로 평균을 빼면 서로 다른 기준이 돼 비교가 안 된다 — PCA 와 같은 규칙."""
    rng = np.random.default_rng(4)
    a, _ = _anisotropic(rng)
    b, _ = _anisotropic(np.random.default_rng(5))
    sa, sb = _session(tmp_path, a, name="a"), _session(tmp_path, b, name="b")
    # b 를 a 기준으로 중심화한 값과 b 자기 기준으로 중심화한 값은 달라야 한다
    vs_ref = E.cosine_map(sb, sa, 18, center=True)["values"]
    vs_self = E.cosine_map(sb, sb, 18, center=True)["values"]
    assert vs_ref != vs_self


def test_centered_kmeans_and_matrix_carry_the_flag(tmp_path):
    rng = np.random.default_rng(6)
    feat, _ = _anisotropic(rng)
    a, b = _session(tmp_path, feat, name="a"), _session(tmp_path, feat * 1.0, name="b")

    km = E.kmeans_labels(a, 3, center=True)
    assert km["centered"] is True and len(km["labels"]) == 64

    raw = E.patch_matrix([a, b], 18)
    cen = E.patch_matrix([a, b], 18, center=True)
    assert cen["centered"] is True and raw["centered"] is False
    assert raw["matrix"][0][1] == pytest.approx(1.0, abs=1e-3)
    assert cen["matrix"][0][1] == pytest.approx(1.0, abs=1e-3)      # 같은 특징이면 그대로 1


def test_centering_lowers_a_background_to_object_matrix_entry(tmp_path):
    """같은 자리의 특징이 이미지 사이에서 얼마나 같은가 — 공통 성분만 같은 것은 낮아져야 한다."""
    rng = np.random.default_rng(7)
    feat, obj = _anisotropic(rng)
    a = _session(tmp_path, feat, name="a")
    other = feat.copy()
    other[40] = feat[18]                              # b 의 40번 자리에 a 의 물체 특징을 놓는다
    b = _session(tmp_path, other, name="b")
    # a 의 배경(40번)과 b 의 40번(물체) 은 공통 성분 때문에 원본으로는 높다
    raw = E.patch_matrix([a, b], 40)["matrix"][0][1]
    cen = E.patch_matrix([a, b], 40, center=True)["matrix"][0][1]
    assert raw > 0.9 and cen < raw - 0.4


# ── 층 선택 · 패딩 유무 (wrapper) ───────────────────────────────────────

def _wrapper():
    sys.path.insert(0, str(_REPO / "wrapper"))
    try:
        import importlib

        import encoder_probe as W
        return importlib.reload(W)
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize("tap,n,expected", [
    ("layer_25", 12, 3), ("layer_50", 12, 6), ("layer_75", 12, 9),
    ("layer_50", 27, 14),           # so400m — 층 수가 달라도 비율이 같은 지점
    ("layer_1", 12, 1),             # 하한: 임베딩(0)이 아니라 1 층
    ("layer_100", 12, 12),
])
def test_layer_taps_are_fractions_of_depth(tap, n, expected):
    assert _wrapper().layer_index(tap, n) == expected


@pytest.mark.parametrize("tap", ["siglip", "connector", "layer_", "layer_x", ""])
def test_non_layer_taps_are_not_layers(tap):
    assert _wrapper().layer_index(tap, 12) is None


def test_center_square_crop_has_no_padding_left():
    W = _wrapper()
    assert W.center_square(480, 640) == (0, 80, 480)      # 4:3 → 양옆 80px 씩
    assert W.center_square(640, 480) == (80, 0, 480)
    assert W.center_square(512, 512) == (0, 0, 512)


def test_the_probe_records_the_conditions_it_ran_under():
    """서로 다른 조건(층·패딩)의 결과를 섞어 비교하지 않으려면 meta 에 남아야 한다."""
    src = (_REPO / "wrapper/encoder_probe.py").read_text()
    code = re.sub(r"#.*", "", src)
    for key in ('"pad_mode"', '"layer_index"', '"n_layers"', '"crop"'):
        assert key in code, f"meta 에 {key} 가 없다"
    assert "output_hidden_states=want_layers" in code, "층 탭이 중간 은닉을 안 꺼낸다"
    assert '"--pad"' in code


# ── 스펙 · 라우터 · 화면 ────────────────────────────────────────────────

def test_smolvla_spec_offers_layer_taps_and_padding_modes_but_act_does_not():
    from app.core import policies as P

    smol = P.SPECS["smolvla"].encoder_probe
    keys = [t.key for t in smol.taps]
    assert {"siglip", "layer_25", "layer_50", "layer_75", "connector"} <= set(keys)
    assert sum(1 for t in smol.taps if t.default) == 1, "기본 tap 이 하나여야 한다"
    assert smol.pad_modes is True
    assert P.SPECS["act"].encoder_probe.pad_modes is False, "ACT 는 패딩이 없다"
    ui = P.ui_spec("smolvla")["encoder_probe"]
    assert ui["pad_modes"] is True and P.ui_spec("act")["encoder_probe"]["pad_modes"] is False


def test_router_exposes_the_four_diagnostics():
    src = (_REPO / "backend/app/routers/encoder.py").read_text()
    assert '"/{sid}/norms"' in src
    assert src.count("center: bool = False") >= 3, "유사도·k-means·행렬이 평균 중심화를 안 받는다"
    assert src.count("pad: str") >= 2, "한 장·여러 장 인코딩이 패딩 모드를 안 받는다"


def test_page_wires_the_toggles_and_keeps_the_runs_comparable():
    page = (_REPO / "frontend/src/pages/EncoderProbePage.tsx").read_text()
    assert "view === 'norm'" in page and "/norms`" in page
    assert "&center=" in page and "setCenter" in page
    assert "probe.pad_modes" in page and "pad: effectivePad" in page
    # 패딩 모드가 다른 결과를 섞어 비교하면 안 된다
    assert "(m.pad_mode || 'pad') !== cur.pad" in page
