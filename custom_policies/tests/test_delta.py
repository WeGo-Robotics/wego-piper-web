"""델타 변환기 (feature/act-delta.md) — LeRobot 으로 만든 작은 합성 데이터셋에서.

1. 관절은 지령의 차분, 첫 프레임은 a[0]−s[0], 그리퍼는 절대 그대로
2. 정규화 통계(stats.json·에피소드 표)가 **델타의 것**이다 — LeRobot 은 학습 때 다시 세지 않는다
3. 사이드카: 델타 채널·스텝당 상한(데모의 최대 한 스텝)
4. LeRobot 로더가 델타를 돌려준다
5. 거절: 이미 델타 / 구운 사본 / 남의 디렉터리 덮어쓰기
"""

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("lerobot")
pq = pytest.importorskip("pyarrow.parquet")

from lerobot.datasets.lerobot_dataset import LeRobotDataset  # noqa: E402

from lerobot_policy_piper import delta as D  # noqa: E402

NAMES = [f"joint{i}.pos" for i in range(1, 7)] + ["gripper.pos"]
T = 12


def _make(root: Path, n_eps=2, seed=0) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """`root` 에 데이터셋을 만들고 에피소드별 (action, state) 를 돌려준다."""
    feats = {"action": {"dtype": "float32", "shape": (7,), "names": NAMES},
             "observation.state": {"dtype": "float32", "shape": (7,), "names": NAMES}}
    ds = LeRobotDataset.create(f"{root.parent.name}/{root.name}", fps=10, features=feats, root=root,
                               use_videos=False)
    rng = np.random.default_rng(seed)
    out = {}
    for e in range(n_eps):
        a = (np.cumsum(rng.normal(size=(T, 7)), 0) * 3).astype(np.float32)
        s = (a + rng.normal(scale=0.2, size=a.shape)).astype(np.float32)   # 추종 지연 흉내
        for t in range(T):
            ds.add_frame({"action": a[t], "observation.state": s[t], "task": "pick"})
        ds.save_episode()
        out[e] = (a.astype(np.float64), s.astype(np.float64))
    ds.finalize()
    return out


@pytest.fixture()
def converted(tmp_path):
    src = tmp_path / "org" / "toy"
    eps = _make(src)
    dst = src.with_name("toy_delta")
    summary = D.convert(src, dst, log=lambda *_: None)
    return src, dst, eps, summary


def _actions(root: Path) -> np.ndarray:
    t = pq.read_table(sorted((root / "data").glob("*/*.parquet"))[0])
    return np.stack(t.column("action").to_pylist()).astype(np.float64)


# ── 1. 값 ───────────────────────────────────────────────────────────────

def test_round_trip_is_exact():
    rng = np.random.default_rng(1)
    a, s0 = rng.normal(size=(20, 7)), rng.normal(size=6)
    idx = list(range(6))
    d = D.to_delta(a, s0, idx)
    assert np.allclose(D.from_delta(d, s0, idx), np.c_[a[:, :6], a[:, 6:]])
    assert np.allclose(d[0, :6], a[0, :6] - s0)
    assert np.allclose(d[:, 6], a[:, 6])                               # 그리퍼는 그대로


def test_joints_become_command_differences_and_the_gripper_stays(converted):
    _, dst, eps, _ = converted
    got = _actions(dst)
    want = []
    for a, s in eps.values():
        d = a.copy()
        d[0, :6] = a[0, :6] - s[0, :6]
        d[1:, :6] = np.diff(a[:, :6], axis=0)
        want.append(d)
    assert np.allclose(got, np.concatenate(want), atol=1e-4)


# ── 2. 통계 ─────────────────────────────────────────────────────────────

def test_normalization_stats_are_the_deltas(converted):
    src, dst, _, _ = converted
    got = _actions(dst)
    stats = json.loads((dst / "meta" / "stats.json").read_text())["action"]
    assert np.allclose(stats["mean"], got.mean(0), atol=1e-4)
    assert np.allclose(stats["std"], got.std(0), atol=1e-3)
    # 다른 feature 의 통계는 손대지 않는다
    before = json.loads((src / "meta" / "stats.json").read_text())
    after = json.loads((dst / "meta" / "stats.json").read_text())
    assert before["observation.state"] == after["observation.state"]
    # 에피소드 표의 통계도 델타의 것이다 (에피소드를 덧붙일 때 aggregate 가 이걸 쓴다)
    ep = pq.read_table(sorted((dst / "meta" / "episodes").glob("*/*.parquet"))[0])
    assert np.allclose(ep.column("stats/action/mean").to_pylist()[0], got[:T].mean(0), atol=1e-4)


# ── 3. 사이드카 ─────────────────────────────────────────────────────────

def test_sidecar_names_the_channels_and_the_step_limit(converted):
    _, dst, eps, summary = converted
    meta = json.loads((dst / "meta" / D.META_FILE).read_text())
    assert meta == json.loads(json.dumps(summary))
    assert meta["action_space"] == "delta_v1"
    assert meta["delta_names"] == NAMES[:6] and meta["absolute_names"] == ["gripper.pos"]
    steps = np.concatenate([np.abs(np.diff(a[:, :6], axis=0)) for a, _ in eps.values()])
    assert np.allclose([meta["clamp"][n] for n in NAMES[:6]], steps.max(0), atol=1e-3)
    assert meta["reconstruction_max_err"] < D.RECON_TOL


# ── 4. 로더 ─────────────────────────────────────────────────────────────

def test_lerobot_loads_the_deltas(converted):
    _, dst, _, _ = converted
    ds = LeRobotDataset(f"{dst.parent.name}/{dst.name}", root=dst)
    assert np.allclose(ds[1]["action"].numpy(), _actions(dst)[1], atol=1e-5)
    assert np.allclose(ds.meta.stats["action"]["mean"], _actions(dst).mean(0), atol=1e-4)


# ── 5. 거절 ─────────────────────────────────────────────────────────────

def test_refuses_to_convert_twice(converted):
    _, dst, _, _ = converted
    with pytest.raises(ValueError, match="이미 델타"):
        D.convert(dst, dst.with_name("x"), verify=False)


def test_refuses_baked_copies(tmp_path):
    src = tmp_path / "org" / "toy_stage"
    _make(src, n_eps=1)
    (src / "meta" / "stage_bake.json").write_text("{}")
    with pytest.raises(ValueError, match="구운 사본"):
        D.convert(src, src.with_name("toy_stage_delta"), verify=False)


def test_force_only_replaces_its_own_output(converted, tmp_path):
    src, dst, _, _ = converted
    with pytest.raises(FileExistsError):
        D.convert(src, dst, verify=False)
    D.convert(src, dst, force=True, verify=False, log=lambda *_: None)    # 우리 것이면 다시 만든다
    foreign = tmp_path / "org" / "someone_elses"
    foreign.mkdir()
    with pytest.raises(FileExistsError, match="결과물이 아니다"):
        D.convert(src, foreign, force=True, verify=False)
    assert foreign.is_dir()


def test_a_failed_conversion_leaves_nothing(converted, monkeypatch):
    src, _, _, _ = converted
    out = src.with_name("toy_broken")

    def boom(*_a, **_k):
        raise RuntimeError("검증 실패 흉내")

    monkeypatch.setattr(D, "_verify", boom)
    with pytest.raises(RuntimeError):
        D.convert(src, out)
    assert not out.exists()
    shutil.rmtree(out, ignore_errors=True)
