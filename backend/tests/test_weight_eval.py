"""가중치 분석 — 오프라인 평가 집계·캐시·배타 (feature/weight-analyzer.md)."""

import json
import os

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import exclusivity as ex
from app.services import weight_eval as WE
from app.services.exclusivity import Activity

_NAMES = [f"joint{i}.pos" for i in range(1, 7)] + ["gripper.pos"]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(WE.settings, "log_dir", tmp_path)
    return tmp_path / "weight_eval"


def _result(store, key, rows, phases=None, monkeypatch=None):
    d = store / key
    d.mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({"key": key, "checkpoint": "run/010000", "dataset": "org/ds",
                                            "episodes": [0], "stride": 1, "started": 1}))
    (d / "meta.json").write_text(json.dumps({"fps": 10, "action_names": _NAMES, "frames": len(rows),
                                             "chunk_size": 4, "elapsed_s": 1.0, "policy_type": "act"}))
    pd.DataFrame(rows).to_parquet(d / "result.parquet")
    if monkeypatch is not None:
        monkeypatch.setattr(WE, "_phase_map", lambda _ds: phases or {})


def _row(ep, fr, pred, true, chunk=None):
    return {"episode": ep, "frame": fr, "pred": list(pred), "true": list(true),
            "chunk_mae": list(chunk if chunk is not None else np.abs(np.array(pred) - np.array(true))),
            "horizon": 4}


def test_errors_are_degrees_for_joints_and_units_for_the_gripper(store, monkeypatch):
    """정규화 1단위가 관절마다 몇 도인지 곱해서 낸다 — 정규화 값 그대로면 관절끼리 못 견준다."""
    true = [0.0] * 7
    pred = [10.0, 0, 0, 0, 0, 0, 30.0]
    _result(store, "k1", [_row(0, 0, pred, true)], monkeypatch=monkeypatch)
    s = WE.summary("k1")

    dims = WE._dims(_NAMES, 7)
    assert [d["kind"] for d in dims] == ["joint"] * 6 + ["gripper"]
    assert all(d["scale"] > 0 for d in dims[:6]) and dims[6]["scale"] == 1.0
    assert s["per_dim"][0] == pytest.approx(10 * dims[0]["scale"], abs=0.01)
    assert s["per_dim"][6] == pytest.approx(30.0)
    assert s["overall"]["joint_deg"] == pytest.approx(10 * dims[0]["scale"] / 6, abs=0.01)


def test_errors_split_by_phase(store, monkeypatch):
    """오차는 페이즈별로 — 한 숫자로 뭉치면 "어디서 틀리나" 가 안 보인다."""
    from piper_phase import APPROACH, GRASP

    rows = [_row(0, f, [1.0] * 7 if f < 2 else [5.0] * 7, [0.0] * 7) for f in range(4)]
    _result(store, "k2", rows, phases={(0, 0): APPROACH, (0, 1): APPROACH, (0, 2): GRASP, (0, 3): GRASP},
            monkeypatch=monkeypatch)
    by = {p["name"]: p for p in WE.summary("k2")["by_phase"]}
    assert set(by) == {"APPROACH", "GRASP"}
    assert by["GRASP"]["joint_deg"] == pytest.approx(5 * by["APPROACH"]["joint_deg"], rel=0.02)  # 둘째 자리 반올림


def test_gripper_timing_pairs_each_recorded_close(store, monkeypatch):
    """녹화가 닫는 순간마다 예측이 몇 초 늦거나 이른가. 2초 안에 없으면 놓침."""
    true = [100, 100, 100, 0, 0, 0, 0, 0, 100, 100]
    pred = [100, 100, 100, 100, 100, 0, 0, 0, 100, 100]      # 2프레임(0.2초) 늦게 닫는다
    rows = [_row(0, f, [0] * 6 + [p], [0] * 6 + [t]) for f, (p, t) in enumerate(zip(pred, true))]
    _result(store, "k3", rows, monkeypatch=monkeypatch)
    s = WE.summary("k3")
    g = s["by_episode"][0]["grip"][0]
    assert g["closes"] == 1 and g["missed"] == 0
    assert g["deltas_s"] == [pytest.approx(0.2)]
    assert s["overall"]["grip_mean_s"] == pytest.approx(0.2)

    never = [100] * 10
    rows = [_row(0, f, [0] * 6 + [p], [0] * 6 + [t]) for f, (p, t) in enumerate(zip(never, true))]
    _result(store, "k4", rows, monkeypatch=monkeypatch)
    assert WE.summary("k4")["overall"]["grip_missed"] == 1


def test_job_key_ignores_episode_order_but_not_new_weights(tmp_path):
    ck = tmp_path / "ck"
    ck.mkdir()
    (ck / "model.safetensors").write_bytes(b"a")
    ds = tmp_path / "ds"
    (ds / "data").mkdir(parents=True)
    (ds / "data" / "f.parquet").write_bytes(b"x")

    a = WE.job_key(ck, ds, [3, 1, 2], 5)
    assert a == WE.job_key(ck, ds, [1, 2, 3], 5)
    assert a != WE.job_key(ck, ds, [1, 2, 3], 3), "표본이 다르면 다른 결과"
    os.utime(ck / "model.safetensors", ns=(1, 1))
    assert a != WE.job_key(ck, ds, [1, 2, 3], 5), "가중치가 바뀌면 캐시를 쓰면 안 된다"


@pytest.fixture
def idle(monkeypatch):
    running: set[Activity] = set()
    monkeypatch.setattr(ex, "STATE_PROVIDERS", {a: (lambda a=a: a in running) for a in ex.STATE_PROVIDERS})
    return running


def test_weight_eval_and_gpu_jobs_block_each_other(idle):
    """GPU 를 쓰는 셋과 서로 막는다 — 한쪽만 막으면 OOM 이 난다 (CLAUDE.md GPU 경합)."""
    for other in (Activity.TRAINING, Activity.INFERENCE, Activity.DETECTOR_TRAIN):
        idle.clear()
        idle.add(other)
        assert other in ex.blocking(Activity.WEIGHT_EVAL)
        idle.clear()
        idle.add(Activity.WEIGHT_EVAL)
        assert Activity.WEIGHT_EVAL in ex.blocking(other)
    assert Activity.WEIGHT_EVAL not in ex.ESTOP_TARGETS, "팔을 안 움직인다"


def test_training_curve_is_saved_to_the_run_folder(tmp_path):
    """레지스트리 레코드는 다음 학습을 시작하면 지워진다 — 실행 폴더에도 남겨야 지난 학습을 견준다."""
    from app.services.training import train_manager

    old = train_manager.output_dir
    try:
        train_manager.output_dir = str(tmp_path)
        train_manager._save_history({"steps": [1000, 2000], "losses": [0.5, 0.3]})
    finally:
        train_manager.output_dir = old
    saved = json.loads((tmp_path / WE.TRAIN_METRICS_FILE).read_text())
    assert saved["losses"] == [0.5, 0.3]


def test_unknown_run_is_404():
    assert TestClient(app).get("/api/weval/run", params={"run": "nope/none"}).status_code == 404


# ── 프레임 꺼내기 (장면 편집기 · 엔코더 프로브의 바탕, W6) ──

def _fake_frames(monkeypatch, store):
    """스크립트 대신 받은 인자를 기록하고 meta.json 만 쓴다 — LeRobot 없이 경로·검증만 본다."""
    seen = {}

    def run(cmd, out):
        seen["cmd"] = cmd
        eps = cmd[cmd.index("--episodes") + 1: cmd.index("--frames")]
        frs = cmd[cmd.index("--frames") + 1: cmd.index("--out")]
        items = [{"episode": int(e), "frame": int(f), "dir": f"e{e}_f{f}"} for e in eps for f in frs]
        for it in items:
            (out / it["dir"]).mkdir()
            (out / it["dir"] / "top.png").write_bytes(b"png")
        meta = {"mode": "frames", "cameras": ["top"], "items": items, "task": "t"}
        (out / "meta.json").write_text(json.dumps(meta))
        return meta

    monkeypatch.setattr(WE, "_run_blocking", run)
    monkeypatch.setattr(WE, "resolve_dataset", lambda _id: store)
    return seen


def test_frames_accepts_one_or_many(store, monkeypatch):
    """장면 편집기는 한 장(episode/frame), 엔코더 프로브는 여러 장(episodes/frames 조합)을 부른다."""
    seen = _fake_frames(monkeypatch, store)
    c = TestClient(app)

    one = c.post("/api/weval/frames", json={"dataset": "org/ds", "episode": 2, "frame": 30}).json()
    assert [it["dir"] for it in one["items"]] == ["e2_f30"]
    png = c.get(f"/api/weval/frames/{one['sid']}/e2_f30/top.png")
    assert png.status_code == 200 and png.content == b"png"

    many = c.post("/api/weval/frames", json={"dataset": "org/ds", "episodes": [0, 3, 3], "frames": [0, 60]}).json()
    assert [it["dir"] for it in many["items"]] == ["e0_f0", "e0_f60", "e3_f0", "e3_f60"], "중복은 한 번만"
    assert seen["cmd"][seen["cmd"].index("--episodes") + 1: seen["cmd"].index("--frames")] == ["0", "3"]


def test_frames_refuses_too_many_and_path_tricks(store, monkeypatch):
    _fake_frames(monkeypatch, store)
    c = TestClient(app)
    r = c.post("/api/weval/frames", json={"dataset": "org/ds", "episodes": list(range(10)), "frames": list(range(10))})
    assert r.status_code == 400, "100장은 한도(48) 밖"
    assert c.post("/api/weval/frames", json={"dataset": "org/ds"}).status_code == 400, "고른 것이 없다"
    for bad in ("../x", ".hidden"):
        with pytest.raises(LookupError):
            WE.frame_png("abc", bad, "top")


# ── 카메라 이름 — 학습 때 rename_map 을 쓴 체크포인트 ─────────────────────────

def _offline_eval():
    import importlib
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "wrapper"))
    try:
        import offline_eval
        return importlib.reload(offline_eval)
    finally:
        sys.path.pop(0)


def test_cameras_are_taken_by_their_dataset_names(tmp_path):
    """**회귀** — smolvla_base 에서 이어 학습한 모델은 `camera1`·`camera2` 로 학습됐다.

    정책의 입력 이름으로 데이터셋을 찾아 `KeyError: 'observation.images.camera1'` 로 죽었다(2026-10-28).
    데이터셋 이름 그대로 넘기면 체크포인트 전처리기의 rename 단계가 바꾼다. base 의 빈 자리(`camera3`)와
    정책이 안 받는 카메라는 고르지 않는다.
    """
    from types import SimpleNamespace

    (tmp_path / "policy_preprocessor.json").write_text(json.dumps({"steps": [
        {"registry_name": "rename_observations_processor", "config": {"rename_map": {
            "observation.images.top": "observation.images.camera1",
            "observation.images.hand": "observation.images.camera2"}}},
        {"registry_name": "to_batch_processor", "config": {}}]}))
    policy = SimpleNamespace(config=SimpleNamespace(input_features={
        "observation.state": 0, "observation.images.camera1": 0,
        "observation.images.camera2": 0, "observation.images.camera3": 0}))
    cams = ["observation.images.top", "observation.images.hand", "observation.images.side"]
    W = _offline_eval()
    assert W.image_keys_for(policy, str(tmp_path), cams) == cams[:2]

    # rename 없이 학습한 것은 이름이 그대로 맞는다
    plain = SimpleNamespace(config=SimpleNamespace(input_features={"observation.images.top": 0}))
    assert W.image_keys_for(plain, str(tmp_path / "none"), cams) == ["observation.images.top"]
