"""에피소드 묶음 (feature/agent-cli.md §4-5) — 에이전트가 보는 자료 하나. 데이터는 읽기만, 판정은 안 한다.

작은 v3 모양 데이터셋을 tmp 에 짓고(parquet·info·episodes·페이즈 사이드카) 프레임 읽기만 가짜로 꽂는다 —
mp4 디코딩은 여기서 검사하지 않는다(실기 스모크의 몫).
"""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import dataset_scanner as DS
from app.services import episode_bundle as EB

JPEG = b"\xff\xd8\xff\xd9"
PHASES = ["IDLE", "APPROACH", "ALIGN", "GRASP", "HOLD", "RELEASE", "DONE", "PARKING"]


@pytest.fixture
def mini_ds(tmp_path, monkeypatch):
    import numpy as np
    import pandas as pd

    ds = tmp_path / "ds"
    (ds / "meta").mkdir(parents=True)
    (ds / "data" / "chunk-000").mkdir(parents=True)
    length = 40
    info = {"codebase_version": "v3.0", "robot_type": "piper_follower_shm", "fps": 10, "chunks_size": 1000,
            "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
            "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
            "features": {"observation.state": {"dtype": "float32", "shape": [3], "names": ["j1", "j2", "gripper"]},
                         "action": {"dtype": "float32", "shape": [3], "names": ["j1", "j2", "gripper"]},
                         "observation.images.top": {"dtype": "video"}, "observation.images.hand": {"dtype": "video"}}}
    (ds / "meta" / "info.json").write_text(json.dumps(info))
    rec = {"episode_index": 0, "length": length, "tasks": ["집어"], "data/chunk_index": 0, "data/file_index": 0,
           "videos/observation.images.top/from_timestamp": 0.0}
    (ds / "meta" / "episodes.jsonl").write_text(json.dumps(rec) + "\n")
    df = pd.DataFrame({
        "episode_index": [0] * length, "frame_index": list(range(length)),
        "timestamp": [i / 10 for i in range(length)],
        "observation.state": [np.array([i, -i, 50.0], dtype=np.float32) for i in range(length)],
        "action": [np.array([i + 1, -i, 40.0], dtype=np.float32) for i in range(length)],
    })
    df.to_parquet(ds / "data" / "chunk-000" / "file-000.parquet")
    monkeypatch.setattr(DS, "episode_meta",
                        lambda dataset_id, episode: (ds, info, rec) if (dataset_id == "u/mini" and episode == 0) else None)
    return ds


def _labels(ds, segments):
    (ds / "meta" / "phase_labels.json").write_text(json.dumps({
        "version": 3, "phases": PHASES,
        "episodes": {"0": {"segments": segments, "reviewed": False, "note": "", "cycles": 1}}}))


def _signals(ds, length, segments):
    import pandas as pd

    def phase_of(i):
        return next((int(v) for s, t, v in segments if s <= i <= t), 0)

    pd.DataFrame({"episode_index": [0] * length, "frame_index": list(range(length)),
                  "speed": [0.1] * length, "gripper_gap": [1.0] * length,
                  "phase": [phase_of(i) for i in range(length)]}).to_parquet(ds / "meta" / "phase_signals.parquet")


def fake_reader(record):
    def reader(ctx, cam_key, frames):
        record.append((cam_key, list(frames)))
        # hand 의 0 번 프레임은 못 읽는 척 — 빠진 프레임이 README 에 적히는지 보려고
        return {f: (JPEG, "jpg") for f in frames if not (cam_key.endswith("hand") and f == 0)}
    return reader


def test_phase_boundaries_pick_frames_before_and_after_each_boundary(mini_ds):
    segs = [[0, 9, 0], [10, 24, 1], [25, 39, 3]]
    _labels(mini_ds, segs)
    _signals(mini_ds, 40, segs)
    rec = []
    data, summary = EB.build("u/mini", 0, frame_reader=fake_reader(rec))
    assert summary["frames"] == [0, 9, 10, 24, 25, 39] and summary["at"] == "phases" and not summary["fallback"]
    assert [c for c, _ in rec] == ["observation.images.top", "observation.images.hand"]
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert {"README.md", "timeline.json", "state_action.csv"} <= names
    assert "keyframes/top/frame-000010.jpg" in names and "keyframes/hand/frame-000000.jpg" not in names
    tl = json.loads(zf.read("timeline.json"))
    assert tl["phases"]["segments"][2] == {"start": 25, "end": 39, "phase": "GRASP"}
    assert tl["keyframes"]["missing"] == {"hand": [0]} and tl["signals"]["phase"][25] == "GRASP"
    assert tl["features"]["observation.state"] == ["j1", "j2", "gripper"] and tl["tasks"] == ["집어"]
    head, *rows = zf.read("state_action.csv").decode().strip().splitlines()
    assert head == "frame_index,timestamp,phase,state.j1,state.j2,state.gripper,action.j1,action.j2,action.gripper"
    assert len(rows) == 40 and rows[25].startswith("25,2.5,GRASP,25.0,-25.0,50.0,26.0,-25.0,40.0")
    readme = zf.read("README.md").decode()
    assert "GRASP" in readme and "hand [0]" in readme and "페이즈 경계마다" in readme and "-100..100" in readme


def test_without_phase_labels_it_falls_back_to_even_sampling_and_says_so(mini_ds):
    data, summary = EB.build("u/mini", 0, frame_reader=fake_reader([]))
    assert summary["fallback"] is True and summary["at"] == "every:2"
    assert summary["frames"][:3] == [0, 2, 4] and summary["frames"][-1] == 39
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert json.loads(zf.read("timeline.json"))["phases"] is None
    assert "페이즈 분석이 없어" in zf.read("README.md").decode()


def test_every_n_cams_filter_and_errors(mini_ds):
    data, summary = EB.build("u/mini", 0, cams=["hand"], at="every:10", frame_reader=fake_reader([]))
    assert summary["frames"] == [0, 10, 20, 30, 39] and summary["cameras"] == ["hand"]
    assert not any(n.startswith("keyframes/top/") for n in zipfile.ZipFile(io.BytesIO(data)).namelist())
    with pytest.raises(EB.BundleError) as ei:
        EB.build("u/mini", 0, cams=["side"], frame_reader=fake_reader([]))
    assert ei.value.status == 400 and "side" in str(ei.value) and "hand" in str(ei.value)
    with pytest.raises(EB.BundleError) as ei:
        EB.build("u/mini", 7, frame_reader=fake_reader([]))
    assert ei.value.status == 404
    with pytest.raises(EB.BundleError) as ei:
        EB.build("u/mini", 0, at="all", frame_reader=fake_reader([]))
    assert ei.value.status == 400
    assert EB.parse_cams(" top, hand ") == ["top", "hand"] and EB.parse_cams(None) is None and EB.parse_cams("") is None


def test_keyframes_are_capped_but_keep_first_and_last():
    t = EB.thin(EB.frames_every(5000, 1))
    assert len(t) <= EB.MAX_KEYFRAMES_PER_CAM and t[0] == 0 and t[-1] == 4999
    assert EB.frames_at_boundaries(40, [[0, 9, 0], [10, 24, 1]]) == [0, 9, 10, 39]
    assert EB.frames_at_boundaries(0, [[0, 1, 0]]) == []
    assert EB.frames_every(20, 5) == [0, 5, 10, 15, 19]


def test_routes_serve_the_zip_and_map_errors(monkeypatch):
    client = TestClient(app)
    calls = []

    def fake_build(dataset_id, episode, *, cams=None, at="phases", frame_reader=None):
        calls.append((dataset_id, episode, cams, at))
        if episode == 9:
            raise EB.BundleError(404, "없다")
        return b"PK\x03\x04zip", {"frames": [0]}

    monkeypatch.setattr(EB, "build", fake_build)
    r = client.get("/api/datasets/u/mini/episodes/3/bundle?cams=top,hand&at=every:5")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip" and r.content.startswith(b"PK")
    assert "ep000003.zip" in r.headers["content-disposition"] and calls[-1] == ("u/mini", 3, ["top", "hand"], "every:5")
    assert client.get("/api/datasets/u/mini/episodes/9/bundle").status_code == 404
    # 외부 API 도 같은 묶음 — 토큰이 걸린다
    monkeypatch.setattr(settings, "api_token", "tok")
    r = client.get("/api/ext/v1/datasets/u/mini/episodes/3/bundle", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert client.get("/api/ext/v1/datasets/u/mini/episodes/3/bundle").status_code == 401


def test_debug_run_zip_can_carry_keyframes(monkeypatch, tmp_path):
    run = tmp_path / "20260101_000000"
    (run / "images" / "top").mkdir(parents=True)
    for i in range(1, 21):
        (run / "images" / "top" / f"{i:06d}.jpg").write_bytes(JPEG)
    (run / "meta.json").write_text("{}")
    monkeypatch.setattr(settings, "debug_dir", str(tmp_path))
    client = TestClient(app)
    r = client.get("/api/debug/runs/20260101_000000/download?keyframes=every:5")
    assert r.status_code == 200, r.text
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "images/top/000001.jpg" in names and "README.md" in names
    assert sorted(n for n in names if n.startswith("keyframes/top/")) == [
        "keyframes/top/000001.jpg", "keyframes/top/000006.jpg", "keyframes/top/000011.jpg",
        "keyframes/top/000016.jpg", "keyframes/top/000020.jpg"]
    plain = zipfile.ZipFile(io.BytesIO(client.get("/api/debug/runs/20260101_000000/download").content)).namelist()
    assert not any(n.startswith("keyframes/") for n in plain) and "README.md" not in plain
    assert client.get("/api/debug/runs/20260101_000000/download?keyframes=bogus").status_code == 400
    assert client.get("/api/debug/runs/20260101_000000/download?keyframes=phases").status_code == 400
