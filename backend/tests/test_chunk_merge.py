"""청크 이어 붙이기 — wrapper/chunk_merge.py 와 lerobot_wrapper 배선.

큐 모드는 새 청크로 큐를 통째로 갈아끼웠다. 팔이 명령보다 늦어 새 청크가 뒤처진 자리에서
시작하므로, 경계마다 명령이 진행 방향 반대로 2~4cm 되돌아갔다(2026-10-28 로그). 여기 테스트는

1. 경계에서 되돌아가지 않는가 — 크로스페이드·정렬·스무딩이 각자 맞는 일을 하는가
2. 끄면 예전과 똑같은가 — 0/0 으로 끈 추론은 한 칸도 달라지면 안 된다 (기본은 10/7 로 켜짐)
3. wrapper 가 정말 그걸 쓰는가 — "설정만 하고 안 쓰는" 슬라이더로 돌아가지 않게
"""

import re
import sys
from pathlib import Path

import numpy as np
import pytest

from conftest import code_only

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "wrapper"))
from chunk_merge import ChunkMerger, savgol  # noqa: E402


def _ramp(start, n, v=1.0, dim=2):
    """등속으로 나아가는 궤적 (n, dim). 진행 방향은 +."""
    return np.array([[start + v * i] * dim for i in range(n)], dtype=float)


# ── 1. 경계 ─────────────────────────────────────────────────────────────

def test_the_boundary_no_longer_steps_back():
    """실제로 본 모양 그대로: 옛 큐는 앞서 있고 새 청크는 3칸 뒤에서 시작한다."""
    old = _ramp(10, 8)                     # 남은 옛 큐 10, 11, … — 직전에 9 를 보냈다
    new = _ramp(10 - 3, 30)                # 뒤처진 상태를 보고 낸 청크
    replaced = new
    merged = ChunkMerger().merge(new, old, elapsed=0, last_sent=np.array([9.0, 9.0]))   # 기본값 그대로

    def first_step(q):
        return q[0, 0] - 9.0

    assert first_step(replaced) < 0, "전제: 갈아끼우면 되돌아간다"
    assert first_step(merged) > 0, "이어 붙이면 진행 방향으로 나아가야 한다"
    assert np.all(np.diff(merged[:, 0]) > 0), "크로스페이드 구간 안에서도 되돌아가지 않는다"


def test_crossfade_hands_over_to_the_new_chunk():
    old, new = _ramp(0, 20, v=0.0), _ramp(5, 20, v=0.0)     # 0 에 서 있다가 5 로
    k = 6
    out = ChunkMerger(blend_steps=k, smooth_window=0).merge(new, old, elapsed=0, last_sent=old[0])
    assert 0 < out[0, 0] < 0.5, "첫 칸은 거의 옛 큐"
    assert np.all(np.diff(out[:k, 0]) > 0)
    np.testing.assert_allclose(out[k:], new[k:])
    assert len(out) == len(new)


def test_a_short_old_queue_is_held_at_its_last_value():
    """옛 큐가 크로스페이드보다 짧으면 마지막 값을 늘인다 — 큐가 비면 팔은 그 값에 서 있다."""
    old, new = np.array([[1.0], [2.0]]), np.full((10, 1), 8.0)
    out = ChunkMerger(blend_steps=5, smooth_window=0).merge(new, old, elapsed=0, last_sent=np.array([0.0]))
    w = 0.5 * (1 + np.cos(np.pi * np.arange(1, 6) / 6))
    prev = np.array([1.0, 2.0, 2.0, 2.0, 2.0])
    np.testing.assert_allclose(out[:5, 0], w * prev + (1 - w) * 8.0)


def test_an_empty_queue_blends_from_what_was_last_sent():
    out = ChunkMerger(blend_steps=4, smooth_window=0).merge(np.full((6, 1), 4.0), [], elapsed=0, last_sent=np.array([0.0]))
    assert 0 < out[0, 0] < 1.0


def test_steps_already_sent_are_dropped():
    """청크의 k 번째는 관측 스텝 + k 의 액션이다. 관측 뒤 3칸이 이미 나갔으면 3번째부터."""
    new = _ramp(0, 10, dim=1)
    out = ChunkMerger(blend_steps=1, smooth_window=0).merge(new, new[:1] * 0 + 3.0, elapsed=3, last_sent=np.array([2.0]))
    assert len(out) == 7
    np.testing.assert_allclose(out[1:], new[4:])


def test_elapsed_never_throws_the_whole_chunk_away():
    out = ChunkMerger(blend_steps=1, smooth_window=0).merge(_ramp(0, 5, dim=1), [[0.0]], elapsed=99, last_sent=np.array([0.0]))
    assert len(out) == 1


def test_smoothing_has_no_lag():
    """2차 Savitzky-Golay 는 2차까지의 궤적을 그대로 지난다 — EMA 처럼 뒤처지지 않는다."""
    t = np.arange(40, dtype=float)
    path = np.stack([0.02 * t**2 - t, 3 * t + 1], axis=1)
    out = ChunkMerger(blend_steps=0, smooth_window=9).merge(path[1:], [], elapsed=0, last_sent=path[0])
    np.testing.assert_allclose(out, path[1:], atol=1e-9)


def test_smoothing_takes_the_shake_out():
    rng = np.random.default_rng(0)
    clean = _ramp(0, 50, v=0.5, dim=3)
    noisy = clean + rng.normal(scale=0.5, size=clean.shape)
    out = ChunkMerger(blend_steps=0, smooth_window=7).merge(noisy, [], elapsed=0, last_sent=clean[0] - 0.5)
    assert np.abs(np.diff(out, 2, axis=0)).mean() < 0.4 * np.abs(np.diff(noisy, 2, axis=0)).mean()


def test_savgol_matches_scipy_including_the_edges():
    signal = pytest.importorskip("scipy.signal")
    x = np.random.default_rng(1).normal(size=(23, 4))
    for w in (5, 7, 11, 15):
        np.testing.assert_allclose(savgol(x, w), signal.savgol_filter(x, w, 2, axis=0, mode="interp"), atol=1e-9)


def test_savgol_shrinks_the_window_to_fit():
    x = np.random.default_rng(2).normal(size=(6, 1))
    signal = pytest.importorskip("scipy.signal")
    np.testing.assert_allclose(savgol(x, 15), signal.savgol_filter(x, 5, 2, axis=0, mode="interp"), atol=1e-9)
    np.testing.assert_array_equal(savgol(x[:4], 15), x[:4])     # 5점이 안 되면 손대지 않는다


def test_kept_dims_are_the_new_chunk_untouched():
    """그리퍼 우회가 켜져 있으면 잡는 타이밍을 뭉개지 않는다."""
    new = np.stack([_ramp(0, 20, dim=1)[:, 0], np.r_[np.zeros(5), np.full(15, 100.0)]], axis=1)
    old = np.full((5, 2), -3.0)
    out = ChunkMerger(blend_steps=8, smooth_window=7).merge(new, old, elapsed=0, last_sent=old[0], keep=[1])
    np.testing.assert_array_equal(out[:, 1], new[:, 1])
    assert not np.allclose(out[:, 0], new[:, 0])


# ── 2. 끄면 예전 그대로 ───────────────────────────────────────────────

def test_off_is_a_plain_replace():
    m = ChunkMerger(blend_steps=0, smooth_window=0)
    assert not m.enabled
    new = _ramp(0, 10)
    np.testing.assert_array_equal(m.merge(new, _ramp(50, 4), elapsed=3, last_sent=np.zeros(2)), new)


def test_the_first_chunk_is_only_smoothed():
    """시작·리셋 직후 — 이어 붙일 옛 큐도 직전 액션도 없다. 정렬·크로스페이드 없이 스무딩만.

    처음엔 그대로 보냈더니 정지에서 출발하는 첫 청크가 정책 원본의 떨림을 실어 한 스텝에 12.8mm 튀었다.
    """
    rng = np.random.default_rng(3)
    clean = _ramp(0, 30, v=0.3, dim=3)
    noisy = clean + rng.normal(scale=0.5, size=clean.shape)
    out = ChunkMerger(blend_steps=10, smooth_window=7).merge(noisy, [], elapsed=8, last_sent=None, keep=[2])
    assert len(out) == len(noisy), "정렬하지 않는다 — 버릴 칸이 없다"
    assert np.abs(np.diff(out[:, :2], 2, axis=0)).mean() < 0.4 * np.abs(np.diff(noisy[:, :2], 2, axis=0)).mean()
    np.testing.assert_array_equal(out[:, 2], noisy[:, 2])          # 그리퍼 우회는 첫 청크도
    # 스무딩이 꺼져 있으면 예전처럼 그대로
    np.testing.assert_array_equal(
        ChunkMerger(blend_steps=10, smooth_window=0).merge(noisy, [], elapsed=8, last_sent=None), noisy)


@pytest.mark.parametrize("value, used", [(0, 0), (3, 0), (4, 0), (5, 5), (6, 7), (7, 7), (8, 9), (99, 9)])
def test_smoothing_window_rules(value, used):
    m = ChunkMerger(blend_steps=0, smooth_window=0)
    assert m.update_param("chunk_smooth_window", value)
    assert m.window() == used
    assert m.enabled == (used > 0)


def test_blend_steps_are_clamped():
    m = ChunkMerger()
    assert m.update_param("chunk_blend_steps", 500) and m.blend_steps == 12
    assert m.update_param("chunk_blend_steps", -2) and m.blend_steps == 0
    assert not m.update_param("lowpass_alpha", 0.5)


# ── 3. 배선 ─────────────────────────────────────────────────────────────

_WRAPPER = (_REPO / "wrapper" / "lerobot_wrapper.py").read_text()


def test_wrapper_merges_instead_of_replacing():
    src = code_only(_WRAPPER)
    assert "from chunk_merge import ChunkMerger" in src
    assert re.search(r"_chunk_merger\.merge\(", src), "추론 결과를 이어 붙이지 않는다"
    # 앙상블이 먼저다 — 앙상블이 켜져 있으면 이어 붙이지 않는다
    assert src.index("_ensembler.add(obs_step") < src.index("elif _chunk_merger.enabled")
    # 정렬 기준은 관측 스텝과 마지막으로 꺼낸 스텝이다
    assert re.search(r"elapsed = _queue_step \+ 1 - obs_step", src) and "elapsed=elapsed" in src


def test_both_param_paths_reach_the_merger():
    """버스(실행 중)와 --config-overrides(시작값) 둘 다 — 하나만 있으면 시작값이 조용히 유실된다."""
    src = code_only(_WRAPPER)
    listener = src[src.index("def param_listener"):src.index("def _resolve_policy_path")]
    assert "_chunk_merger.update_param(key, value)" in listener
    overrides = src[src.index("for key, value in config_overrides.items():"):src.index("preprocessor = None")]
    assert "_chunk_merger.update_param(key, value)" in overrides


def test_the_step_marker_follows_the_queue_and_resets():
    src = code_only(_WRAPPER)
    # 꺼내는 락 안에서, 모드와 상관없이 — 앙상블에서 큐로 돌아온 뒤에도 낡은 값이 남지 않게
    assert re.search(r"with _action_lock:\s*_queue_step = step\s*if _te_active:", src)
    reset = src[src.index("if _reset_requested:"):src.index("if _paused:")]
    assert "_queue_step = -1" in reset


def test_spec_carries_both_and_matches_the_wrapper():
    """화면 기본값·상한(스펙)과 wrapper 의 기본값·클램프가 같아야 한다 — 시작값을 못 받은 경로
    (화면 밖에서 띄운 추론)가 화면과 다르게 돌거나, 화면이 허용한 값을 wrapper 가 몰래 자르지 않게."""
    import chunk_merge as CM
    from app.core.cli_mapping import OVERRIDE_KEYS
    from app.core.inference_params import PARAM_SPEC
    from app.services.param_bridge import SAFE_PARAMS

    for key, default, top in (("chunk_blend_steps", CM.DEFAULT_BLEND, CM.MAX_BLEND),
                              ("chunk_smooth_window", CM.DEFAULT_WINDOW, CM.MAX_WINDOW)):
        assert PARAM_SPEC[key]["default"] == default
        assert PARAM_SPEC[key]["max"] == top
        assert key in SAFE_PARAMS and key in OVERRIDE_KEYS
        assert not PARAM_SPEC[key].get("scoped"), "정책과 무관하다 — 모든 정책의 큐 모드에서 돈다"
    m = CM.ChunkMerger()
    assert m.enabled and m.blend_steps == 10 and m.window() == 7


def test_saved_values_are_clamped_to_the_spec_on_screen():
    """상한을 낮추면(20→12) 예전에 저장한 값이 슬라이더 밖에 남아 그대로 실려 간다."""
    src = (_REPO / "frontend" / "src" / "pages" / "InferencePage.tsx").read_text()
    assert re.search(r"Math\.min\(s\.max, Math\.max\(s\.min, v\)\)", src)
