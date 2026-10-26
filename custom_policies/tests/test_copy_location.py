"""굽기·델타 사본이 **어디에** 생기나 — 실기 사고(2026-10-25, `wego-mink/singlearm_apple_random_v2`).

HF 허브에서 받은 데이터셋을 구웠더니 사본이 `snapshots/<해시>_stage` 에 생겼다. 데이터셋 목록은 "가장 최근
스냅샷" 을 그 이름의 데이터셋으로 골라 **원본 이름이 구운 사본을 가리켰고**, 편집기의 읽기 전용 잠금도 안
걸려 라벨 수정이 사본에만 들어갔다. 사본 안의 영상은 blob 을 가리키는 **상대** 심링크라 옮기면 깨졌다.
그리고 검토한 에피소드가 0개인데 `--reviewed-only` 로 구워 라벨이 하나도 없었고, 그걸로 20,000 스텝을 학습했다.
"""

import json
import os
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("lerobot")
pytest.importorskip("pyarrow")

from lerobot_policy_piper import bake as B  # noqa: E402


def _hub_snapshot(tmp_path: Path) -> Path:
    """HF 캐시 꼴: `datasets--org--name/snapshots/<해시>/…` 와 상대 링크로 된 영상."""
    repo = tmp_path / "hub" / "datasets--wego-mink--apple_v2"
    blob = repo / "blobs" / "ff2e"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"video-bytes")
    snap = repo / "snapshots" / ("a" * 40)
    vid = snap / "videos" / "observation.images.top" / "chunk-000" / "file-000.mp4"
    vid.parent.mkdir(parents=True)
    os.symlink(os.path.relpath(blob, vid.parent), vid)
    return snap


def test_copies_of_hub_datasets_go_to_the_lerobot_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_LEROBOT_HOME", str(tmp_path / "lerobot"))
    snap = _hub_snapshot(tmp_path)
    assert B.repo_id_of(snap) == "wego-mink/apple_v2"
    assert B.default_output(snap, "_stage") == tmp_path / "lerobot" / "wego-mink" / "apple_v2_stage"
    from lerobot_policy_piper import delta as D

    assert B.default_output(snap, D.SUFFIX) == tmp_path / "lerobot" / "wego-mink" / "apple_v2_delta"
    # LeRobot 캐시의 데이터셋은 예전처럼 옆에
    local = tmp_path / "lerobot" / "org" / "cube"
    assert B.default_output(local, "_stage") == local.with_name("cube_stage")
    assert B.repo_id_of(local) == "org/cube"


def test_linked_videos_survive_being_somewhere_else(tmp_path):
    """링크를 따라가 실제 파일에 건다 — 상대 링크를 그대로 옮기면 다른 깊이에서 깨진다."""
    snap = _hub_snapshot(tmp_path)
    src = next((snap / "videos").rglob("*.mp4"))
    dst = tmp_path / "lerobot" / "wego-mink" / "apple_v2_stage" / "videos" / "top" / "file-000.mp4"
    mode = B._link_or_copy(src, dst)
    assert mode == "hardlink" and not dst.is_symlink()
    moved = tmp_path / "elsewhere" / "file-000.mp4"
    moved.parent.mkdir()
    dst.rename(moved)
    assert moved.read_bytes() == b"video-bytes"


def _labels_dataset(tmp_path: Path, reviewed: bool) -> Path:
    import torch  # noqa: F401  — torchcodec 로드 순서
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    names = [f"joint{i}.pos" for i in range(1, 7)] + ["gripper.pos"]
    feats = {"action": {"dtype": "float32", "shape": (7,), "names": names},
             "observation.state": {"dtype": "float32", "shape": (7,), "names": names}}
    root = tmp_path / "org" / "toy"
    ds = LeRobotDataset.create("org/toy", fps=10, features=feats, root=root, use_videos=False)
    for _ in range(8):
        ds.add_frame({"action": np.zeros(7, np.float32), "observation.state": np.zeros(7, np.float32), "task": "t"})
    ds.save_episode()
    ds.finalize()
    (root / "meta" / "phase_labels.json").write_text(json.dumps({
        "phases": ["IDLE", "HOLD"], "episodes": {"0": {"segments": [[0, 7, 1]], "frames": 8, "reviewed": reviewed}}}))
    return root


def test_a_bake_that_would_leave_no_labels_is_refused_and_keeps_the_old_copy(tmp_path):
    src = _labels_dataset(tmp_path, reviewed=False)
    dst = src.with_name("toy_stage")
    B.bake(src, dst, verify=False, log=lambda *_: None)                     # 전부 포함 — 된다
    with pytest.raises(ValueError, match="라벨이 하나도"):
        B.bake(src, dst, reviewed_only=True, force=True, verify=False, log=lambda *_: None)
    assert (dst / "meta" / B.META_FILE).is_file(), "거절하면서 이전 사본을 지웠다"
