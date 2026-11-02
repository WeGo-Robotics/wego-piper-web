"""목표 위치 입력 변환기 (feature/target-conditioning.md) — LeRobot 으로 만든 작은 합성 데이터셋에서.

1. 인코딩 — 가까운 자리는 비슷하고 먼 자리는 갈린다, 목표 없음은 0, 사양만으로 다시 만들 수 있다
2. state 뒤에 목표 칸이 붙고 앞쪽(로봇 state)·액션은 한 비트도 안 바뀐다
3. 통계 — 앞쪽은 원본 그대로, 새 칸은 전 프레임의 것
4. LeRobot 로더가 긴 state 를 돌려준다
5. 거절: 두 번 / 구운 사본 / 표에 없는 에피소드 / 상수 목표 / 남의 디렉터리
"""

import json
import math
import shutil
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("lerobot")
pq = pytest.importorskip("pyarrow.parquet")

from lerobot.datasets.lerobot_dataset import LeRobotDataset  # noqa: E402

from lerobot_policy_piper import target as TG  # noqa: E402

NAMES = [f"joint{i}.pos" for i in range(1, 7)] + ["gripper.pos"]
T = 10
TARGETS = {0: (0.30, 0.55), 1: (0.70, 0.40), 2: None}


def _make(root: Path, n_eps=3, seed=0) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    feats = {"action": {"dtype": "float32", "shape": (7,), "names": NAMES},
             "observation.state": {"dtype": "float32", "shape": (7,), "names": NAMES}}
    ds = LeRobotDataset.create(f"{root.parent.name}/{root.name}", fps=10, features=feats, root=root,
                               use_videos=False)
    rng = np.random.default_rng(seed)
    out = {}
    for e in range(n_eps):
        a = (np.cumsum(rng.normal(size=(T, 7)), 0) * 3).astype(np.float32)
        s = (a + rng.normal(scale=0.2, size=a.shape)).astype(np.float32)
        for t in range(T):
            ds.add_frame({"action": a[t], "observation.state": s[t], "task": "pick"})
        ds.save_episode()
        out[e] = (a, s)
    ds.finalize()
    return out


def _targets_file(path: Path, targets: dict, mm_per_px=1.0) -> Path:
    doc = {"version": 1, "camera": "observation.images.top", "camera_id": "rs:1:color", "frame_size": [640, 480],
           "mm_per_px": mm_per_px,
           "episodes": {str(e): ({"valid": False} if t is None else {"valid": True, "u": t[0], "v": t[1]})
                        for e, t in targets.items()}}
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture()
def converted(tmp_path):
    src = tmp_path / "org" / "toy"
    eps = _make(src)
    dst = src.with_name("toy_target")
    sidecar = TG.convert(src, dst, _targets_file(tmp_path / "t.json", TARGETS), log=lambda *_: None)
    return src, dst, eps, sidecar


def _col(root: Path, name: str) -> np.ndarray:
    t = pq.read_table(sorted((root / "data").glob("*/*.parquet"))[0])
    return np.stack(t.column(name).to_pylist()).astype(np.float64)


def _spec(**kw):
    base = dict(freqs=7, length_mm=28.0, mm_per_px=1.0, frame=(640, 480), with_valid=True, state_names=NAMES)
    return TG.make_spec(**{**base, **kw})


# ── 1. 인코딩 ───────────────────────────────────────────────────────────

def _sim(spec, p, q) -> float:
    """푸리에 칸만의 정규화 내적 — 커널 근사."""
    m = len(spec["freqs"])
    a, b = TG.encode(p, spec)[-2 * m:], TG.encode(q, spec)[-2 * m:]
    return float(a @ b) / m


def test_near_places_look_alike_and_far_places_do_not():
    spec = _spec()                                     # 1 mm/px, 프레임 폭 640 → ℓ = 28px
    p = (0.5, 0.5)
    px = lambda d: (0.5 + d / 640, 0.5)               # noqa: E731 — 오른쪽으로 d px
    near, half, far = _sim(spec, p, px(10)), _sim(spec, p, px(20)), _sim(spec, p, px(80))
    assert near > half > 0.6 > far
    assert abs(half - math.exp(-20 ** 2 / (2 * 28 ** 2))) < 0.08     # 반 칸(20mm) ≈ 0.77
    assert abs(far) < 0.25


def test_the_kernel_is_isotropic_through_the_aspect_ratio():
    """세로 d px 와 가로 d px 가 같은 거리여야 한다 — v 는 높이로 나눈 값이라 종횡비로 되돌린다."""
    spec = _spec()
    p = (0.5, 0.5)
    right = _sim(spec, p, (0.5 + 20 / 640, 0.5))
    down = _sim(spec, p, (0.5, 0.5 + 20 / 480))
    assert abs(right - down) < 0.08


def test_no_target_is_all_zeros_and_needs_the_valid_slot():
    spec = _spec()
    assert spec["names"][0] == TG.VALID and len(spec["names"]) == 3 + 2 * 7
    assert not TG.encode(None, spec).any()
    v = TG.encode((0.2, 0.3), spec)
    assert v[0] == 1.0 and np.allclose(v[1:3], [0.2, 0.3])
    without = _spec(with_valid=False)
    assert without["names"][0] == "target.u"
    with pytest.raises(ValueError, match="목표 없음"):
        TG.encode(None, without)


def test_frequencies_are_deterministic_and_follow_the_length():
    a, b = TG.frequencies(7, 0.05), TG.frequencies(7, 0.05)
    assert np.array_equal(a, b)
    wide = TG.frequencies(7, 0.10)
    assert np.allclose(np.linalg.norm(wide, axis=1) * 2, np.linalg.norm(a, axis=1))   # 폭이 두 배면 주파수는 절반
    assert TG.frequencies(0, 0.05).shape == (0, 2)


# ── 2. 값 ───────────────────────────────────────────────────────────────

def test_targets_are_appended_and_nothing_else_moves(converted):
    src, dst, eps, sidecar = converted
    spec = sidecar["spec"]
    st, act = _col(dst, "observation.state"), _col(dst, "action")
    assert st.shape == (3 * T, 7 + len(spec["names"]))
    assert np.array_equal(st[:, :7], _col(src, "observation.state"))
    assert np.array_equal(act, _col(src, "action"))
    for e, tgt in TARGETS.items():
        rows = st[e * T:(e + 1) * T, 7:]
        assert np.allclose(rows, TG.encode(tgt, spec), atol=1e-6)    # 에피소드 끝까지 같은 목표


def test_info_and_parquet_metadata_carry_the_new_width(converted):
    _, dst, _, sidecar = converted
    info = json.loads((dst / "meta" / "info.json").read_text())
    feat = info["features"]["observation.state"]
    assert feat["shape"] == [7 + 17] and feat["names"] == NAMES + sidecar["spec"]["names"]
    t = pq.read_table(sorted((dst / "data").glob("*/*.parquet"))[0])
    hf = json.loads(t.schema.metadata[b"huggingface"])
    assert hf["info"]["features"]["observation.state"]["length"] == 24


# ── 3. 통계 ─────────────────────────────────────────────────────────────

def test_robot_stats_stay_and_target_stats_are_exact(converted):
    src, dst, _, _ = converted
    before = json.loads((src / "meta" / "stats.json").read_text())["observation.state"]
    after = json.loads((dst / "meta" / "stats.json").read_text())["observation.state"]
    for k, v in before.items():
        if k == "count":
            assert after[k] == v
        else:
            assert after[k][:7] == v                    # 기준 학습과 정규화가 비트 단위로 같다
    st = _col(dst, "observation.state")
    assert np.allclose(after["mean"][7:], st[:, 7:].mean(0), atol=1e-5)
    assert np.allclose(after["std"][7:], st[:, 7:].std(0), atol=1e-4)
    ep = pq.read_table(sorted((dst / "meta" / "episodes").glob("*/*.parquet"))[0])
    m0 = ep.column("stats/observation.state/mean").to_pylist()[0]
    assert len(m0) == 24 and np.allclose(m0[7:], st[0, 7:], atol=1e-6)


# ── 4. 로더·사이드카 ───────────────────────────────────────────────────

def test_lerobot_loads_the_longer_state(converted):
    _, dst, _, sidecar = converted
    ds = LeRobotDataset(f"{dst.parent.name}/{dst.name}", root=dst)
    item = ds[T + 3]                                    # 에피소드 1
    assert item["observation.state"].shape == (24,)
    assert np.allclose(item["observation.state"][7:].numpy(), TG.encode(TARGETS[1], sidecar["spec"]), atol=1e-6)
    assert len(ds.meta.stats["observation.state"]["mean"]) == 24


def test_sidecar_holds_the_whole_spec(converted):
    _, dst, _, sidecar = converted
    meta = json.loads((dst / "meta" / TG.META_FILE).read_text())
    assert meta == json.loads(json.dumps(sidecar))
    assert meta["input"] == "rff_v1" and meta["invalid_episodes"] == [2]
    src_copy = json.loads((dst / "meta" / TG.SOURCE_FILE).read_text())
    assert src_copy["episodes"]["2"] == {"valid": False}                 # 라벨러의 표가 그대로 따라온다
    assert meta["spec"]["state_names"] == NAMES and meta["spec"]["aspect"] == 0.75
    # 사양만으로 같은 벡터를 다시 만든다 — 추론이 하는 일
    assert np.allclose(TG.encode(TARGETS[0], meta["spec"]), _col(dst, "observation.state")[0, 7:], atol=1e-6)


def test_without_empty_episodes_there_is_no_valid_slot(tmp_path):
    src = tmp_path / "org" / "full"
    _make(src, n_eps=2)
    sidecar = TG.convert(src, src.with_name("full_target"),
                         _targets_file(tmp_path / "t.json", {0: (0.3, 0.5), 1: (0.6, 0.4)}),
                         verify=False, log=lambda *_: None)
    assert sidecar["spec"]["names"][0] == "target.u" and len(sidecar["spec"]["names"]) == 16


# ── 5. 거절 ─────────────────────────────────────────────────────────────

def test_refuses_to_convert_twice(converted, tmp_path):
    _, dst, _, _ = converted
    with pytest.raises(ValueError, match="이미 목표"):
        TG.convert(dst, dst.with_name("x"), _targets_file(tmp_path / "t2.json", TARGETS), verify=False)


def test_refuses_baked_copies(tmp_path):
    src = tmp_path / "org" / "toy_stage"
    _make(src, n_eps=1)
    (src / "meta" / "stage_bake.json").write_text("{}")
    with pytest.raises(ValueError, match="구운 사본"):
        TG.convert(src, src.with_name("toy_stage_target"), _targets_file(tmp_path / "t.json", {0: (0.3, 0.3)}),
                   verify=False)


def test_refuses_a_table_that_misses_episodes(tmp_path):
    src = tmp_path / "org" / "toy"
    _make(src)
    with pytest.raises(ValueError, match="목표 표에 없는 에피소드"):
        TG.convert(src, src.with_name("toy_target"), _targets_file(tmp_path / "t.json", {0: (0.3, 0.3)}),
                   verify=False)
    assert not src.with_name("toy_target").exists()


def test_refuses_constant_targets(tmp_path):
    src = tmp_path / "org" / "toy"
    _make(src, n_eps=2)
    with pytest.raises(ValueError, match="상수"):
        TG.convert(src, src.with_name("toy_target"),
                   _targets_file(tmp_path / "t.json", {0: (0.3, 0.3), 1: (0.3, 0.3)}), verify=False)


def test_refuses_out_of_range_targets(tmp_path):
    src = tmp_path / "org" / "toy"
    _make(src, n_eps=1)
    with pytest.raises(ValueError, match="0~1 밖"):
        TG.convert(src, src.with_name("toy_target"), _targets_file(tmp_path / "t.json", {0: (1.3, 0.3)}),
                   verify=False)


def test_force_only_replaces_its_own_output(converted, tmp_path):
    src, dst, _, _ = converted
    tf = _targets_file(tmp_path / "t3.json", TARGETS)
    with pytest.raises(FileExistsError):
        TG.convert(src, dst, tf, verify=False)
    TG.convert(src, dst, tf, force=True, verify=False, log=lambda *_: None)
    foreign = tmp_path / "org" / "someone_elses"
    foreign.mkdir()
    with pytest.raises(FileExistsError, match="결과물이 아니다"):
        TG.convert(src, foreign, tf, force=True, verify=False)
    assert foreign.is_dir()


def test_a_failed_conversion_leaves_nothing(converted, monkeypatch, tmp_path):
    src, _, _, _ = converted
    out = src.with_name("toy_broken")

    def boom(*_a, **_k):
        raise RuntimeError("검증 실패 흉내")

    monkeypatch.setattr(TG, "_verify", boom)
    with pytest.raises(RuntimeError):
        TG.convert(src, out, _targets_file(tmp_path / "t4.json", TARGETS))
    assert not out.exists()
    shutil.rmtree(out, ignore_errors=True)
