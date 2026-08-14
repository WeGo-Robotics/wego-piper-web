"""페이즈 라벨러 (feature/01-phase-annotation.md 1~2단계).

**가장 중요한 불변식은 인과성이다.** 학습 데이터의 페이즈 값이 추론 시에도 채워져야 하므로,
오프라인 라벨러와 온라인 추정기가 같은 결과를 내야 한다. 어긋나면 정책이 학습 때 못 본
입력을 받는다 (§2.4 privileged information).
"""

import json

import numpy as np
import pytest
from piper_phase import (
    APPROACH, DONE, GRASP, HOLD, IDLE, PHASE_NAMES, RELEASE,
    Params, PhaseFSM, compute_signals, count_cycles, finalize, label_causal,
    label_episode, segments,
)

from piper_phase import labeler as PL

from app.services.dataset_scanner import find_dataset_path

_P = Params(fps=15.0)


def _synth_cycle(n_approach=30, n_grasp=10, n_hold=40, n_release=10):
    """합성 에피소드 한 사이클: 이동 → 집기 → 이동 → 놓기."""
    state, action = [], []

    def push(joint_step, g_state, g_cmd, n):
        for _ in range(n):
            j = len(state) * joint_step
            state.append([j, 0, 0, 0, 0, 0, g_state])
            action.append([j, 0, 0, 0, 0, 0, g_cmd])

    push(3.0, 100, 100, n_approach)     # 이동 (열림)
    for i in range(n_grasp):            # 닫히는 중
        g = 100 - (100 - 34) * (i + 1) / n_grasp
        state.append([0, 0, 0, 0, 0, 0, g])
        action.append([0, 0, 0, 0, 0, 0, 0])
    push(3.0, 34, 0, n_hold)            # 물체 물고 이동 (gap = -34)
    for i in range(n_release):          # 열리는 중
        g = 34 + (100 - 34) * (i + 1) / n_release
        state.append([0, 0, 0, 0, 0, 0, g])
        action.append([0, 0, 0, 0, 0, 0, 100])
    push(3.0, 100, 100, n_approach)     # 다음 사이클로
    return np.array(state, float), np.array(action, float)


# ── 인과성 — 이 파일에서 제일 중요한 테스트 ──

def test_offline_causal_core_equals_online_streaming():
    """오프라인 라벨러를 "프레임 t까지만 보이게" 잘라 돌린 결과와
    온라인 추정기 결과가 **완전히 일치**해야 한다.

    어긋나면 미래를 보는 조건이 인과 코어에 섞인 것이다.
    """
    state, action = _synth_cycle()
    sig = compute_signals(state, action, _P)
    offline = label_causal(sig, _P)

    fsm = PhaseFSM(_P)
    online = np.array([
        fsm.update(float(sig.speed[i]), float(sig.gripper_cmd[i]),
                   float(sig.gripper_state[i]), float(sig.grip_rate[i]))
        for i in range(len(sig))
    ], dtype=np.int8)
    assert np.array_equal(offline, online)


def test_online_never_emits_done():
    """추론 중엔 미션 종료를 알 수 없다 — `DONE` 은 오프라인 전용."""
    state, action = _synth_cycle()
    sig = compute_signals(state, action, _P)
    assert DONE not in set(label_causal(sig, _P).tolist())


def test_finalize_is_the_only_future_reader():
    """`finalize` 만 미래를 본다 — 인과 결과와 다른 지점이 꼬리여야 한다."""
    state, action = _synth_cycle()
    sig = compute_signals(state, action, _P)
    causal = label_causal(sig, _P)
    final = finalize(causal, sig, _P)
    diff = np.flatnonzero(causal != final)
    if len(diff):
        # 바뀐 곳은 DONE 이거나 짧은 구간 흡수뿐
        assert set(final[diff].tolist()) <= {DONE} | set(causal.tolist())


# ── 신호 ──

def test_gripper_gap_detects_hold_without_vision():
    """지령 0(완전 닫힘)인데 실측이 34에서 멈추면 물체가 물려 있다.

    **비전 없이 "집기 성공"을 판정하는 신호**라 FSM 의 척추다.
    """
    state, action = _synth_cycle()
    sig = compute_signals(state, action, _P)
    assert sig.hold.any()
    assert sig.gripper_gap.min() < -30


def test_speed_uses_all_six_joints():
    state = np.zeros((10, 7))
    state[:, 3] = np.arange(10) * 2.0
    sig = compute_signals(state, state.copy(), _P)
    assert sig.speed[1:].min() > 0, "joint4 움직임이 속도에 반영 안 됨"


def test_rejects_wrong_shape():
    with pytest.raises(ValueError):
        compute_signals(np.zeros((10, 3)), np.zeros((10, 3)), _P)


# ── FSM 동작 ──

def test_single_cycle_reaches_hold_and_release():
    state, action = _synth_cycle()
    phases, _ = label_episode(state, action, _P)
    seen = set(phases.tolist())
    assert HOLD in seen and RELEASE in seen and GRASP in seen


def test_phases_cycle_not_monotonic():
    """페이즈는 단조 증가가 아니라 **순환**한다 —
    이걸 놓치면 첫 사이클 이후 전부 "완료"가 된다."""
    s1, a1 = _synth_cycle()
    s2, a2 = _synth_cycle()
    state, action = np.vstack([s1, s2]), np.vstack([a1, a2])
    phases, _ = label_episode(state, action, _P)
    assert count_cycles(phases) == 2, f"2사이클이어야 하는데 {count_cycles(phases)}"


def test_segments_roundtrip():
    phases = np.array([0, 0, 1, 1, 1, 4, 4, 6], dtype=np.int8)
    rebuilt = np.concatenate([np.full(t - s + 1, v) for s, t, v in segments(phases)])
    assert np.array_equal(rebuilt, phases)


def test_params_are_all_configurable():
    """임계값은 로봇·태스크마다 다르다 — 하드코딩이 남아 있으면 안 된다."""
    from dataclasses import fields
    names = {f.name for f in fields(Params)}
    for key in ("still_speed", "moving_speed", "hold_gap", "n_hold", "done_still", "fps"):
        assert key in names


# ── 실제 데이터셋 (문서가 측정한 바로 그 데이터) ──

_REAL = "wego-hansu/min_cube_071410"


@pytest.fixture(scope="module")
def real_ds():
    path = find_dataset_path(_REAL)
    if path is None:
        pytest.skip(f"{_REAL} 없음")
    return path


def test_real_dataset_shape_matches_doc(real_ds):
    info = json.loads((real_ds / "meta/info.json").read_text())
    assert info["fps"] == 15
    assert info["total_episodes"] == 50
    assert info["total_frames"] == 31349
    assert info["features"]["observation.state"]["shape"] == [7]


def test_real_dataset_every_episode_has_three_cycles(real_ds):
    """문서의 핵심 주장 — 큐브 3개 = 3사이클이 **전 에피소드에서** 검출돼야 한다."""
    result = PL.analyze(real_ds, Params(fps=PL.dataset_fps(real_ds)))
    cycles = [v["cycles"] for v in result["episodes"].values()]
    assert len(cycles) == 50
    assert set(cycles) == {3}, f"사이클 분포가 {set(cycles)}"


def test_real_gripper_gap_matches_measured_values(real_ds):
    """ep0 의 갭 min/mean = -35.0 / -13.3 (문서 실측값)."""
    df = PL.load_frames(real_ds)
    e = df[df.episode_index == 0]
    sig = compute_signals(np.stack(e["observation.state"].to_numpy()),
                          np.stack(e["action"].to_numpy()), Params(fps=15.0))
    assert round(float(sig.gripper_gap.min()), 1) == -35.0
    assert round(float(sig.gripper_gap.mean()), 1) == -13.3
    assert len(e) == 830


def test_load_frames_reads_every_parquet_chunk():
    """⚠ chunk 가 여러 파일로 나뉘는 데이터셋이 있다 —
    첫 파일만 읽으면 에피소드 절반이 조용히 빠진다."""
    path = find_dataset_path("wego-hansu/yeonwonju-0709-team1")
    if path is None:
        pytest.skip("데이터셋 없음")
    files = sorted((path / "data").rglob("*.parquet"))
    df = PL.load_frames(path)
    info = json.loads((path / "meta/info.json").read_text())
    assert len(files) > 1, "여러 chunk 데이터셋이 아니라 이 테스트가 의미 없음"
    assert df.episode_index.nunique() == info["total_episodes"]


# ── 사이드카 ──

def test_sidecar_roundtrip_and_outliers(real_ds, tmp_path, monkeypatch):
    """원본을 건드리지 않고 사이드카에만 쓴다."""
    result = PL.analyze(real_ds, Params(fps=15.0), episodes=[0, 1, 2])
    s = PL.summary({**result, "_signals": None})
    assert s["episodes"] == 3 and s["median_cycles"] == 3

    # 실제 저장은 tmp 로 (원본 오염 방지)
    fake = tmp_path / "ds"
    (fake / "meta").mkdir(parents=True)
    labels, signals = PL.save(fake, result)
    assert labels.exists() and signals and signals.exists()
    back = PL.load(fake)
    assert back["version"] == PL.SIDECAR_VERSION
    assert back["phases"] == list(PHASE_NAMES)
    assert set(back["episodes"]) == {"0", "1", "2"}
    assert back["params"]["hold_gap"] == -15.0


def test_reanalysis_preserves_review_state(tmp_path):
    """재분석에 사람이 검토한 표시가 날아가면 안 된다."""
    fake = tmp_path / "ds"
    (fake / "meta").mkdir(parents=True)
    first = {"version": 1, "phases": list(PHASE_NAMES), "params": {},
             "episodes": {"0": {"segments": [], "cycles": 3, "frames": 10,
                                "reviewed": True, "edited_by": "auto+manual", "note": "확인함"}}}
    PL.save(fake, {**first, "_signals": None})

    second = {"version": 1, "phases": list(PHASE_NAMES), "params": {},
              "episodes": {"0": {"segments": [], "cycles": 3, "frames": 10,
                                 "reviewed": False, "edited_by": "auto", "note": ""}}}
    PL.save(fake, {**second, "_signals": None})
    back = PL.load(fake)["episodes"]["0"]
    assert back["reviewed"] is True and back["note"] == "확인함"


def test_outliers_flag_zero_cycle_episodes():
    result = {"episodes": {
        "0": {"cycles": 3, "segments": [[0, 9, IDLE], [10, 19, APPROACH], [20, 29, HOLD], [30, 39, DONE]]},
        "1": {"cycles": 3, "segments": [[0, 9, IDLE], [10, 19, APPROACH], [20, 29, HOLD], [30, 39, DONE]]},
        "2": {"cycles": 0, "segments": [[0, 39, IDLE]]},
    }}
    flags = {f["episode"]: f["reasons"] for f in PL.flag_outliers(result)}
    assert 2 in flags and any("미검출" in r for r in flags[2])
    assert 0 not in flags
