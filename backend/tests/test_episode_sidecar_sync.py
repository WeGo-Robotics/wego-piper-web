"""에피소드 삭제 ↔ 사이드카 동기화 (feature/episode-editor.md §4 의 ⚠ 버그).

lerobot 의 in-place delete 는 원본을 `_old` 로 옮기고 meta 를 새로 쓴다 —
사이드카를 안 가져오면 유실이고, 손으로 복사하면 번호가 밀린다.
`piper_phase.sidecar.remap_after_delete` 가 그 빈틈을 메운다.
"""

import json

import pandas as pd
import pytest

from app.core import cli_mapping
from app.core.cli_mapping import build_edit_dataset_args
from piper_phase.sidecar import _episode_mapping, remap_after_delete


def test_episode_mapping_matches_lerobot_rule():
    # 5개 중 1·3 삭제 → 남은 0,2,4 가 0,1,2 로 당겨진다
    assert _episode_mapping(5, [1, 3]) == {0: 0, 2: 1, 4: 2}
    assert _episode_mapping(3, []) == {0: 0, 1: 1, 2: 2}


@pytest.fixture
def edited_dataset(tmp_path):
    """in-place delete 직후 모양: 새 meta(사이드카 없음) + `_old` 백업(사이드카 보유)."""
    ds = tmp_path / "myset"
    old_meta = tmp_path / "myset_old" / "meta"
    new_meta = ds / "meta"
    old_meta.mkdir(parents=True)
    new_meta.mkdir(parents=True)

    (old_meta / "info.json").write_text(json.dumps({"total_episodes": 5}))
    (old_meta / "phase_labels.json").write_text(json.dumps({
        "version": 1,
        "phases": ["IDLE"],
        "episodes": {str(i): {"segments": [[0, 10, 0]], "cycles": i} for i in range(5)},
    }))
    pd.DataFrame({
        "episode_index": [0, 0, 1, 2, 3, 4],
        "frame_index": [0, 1, 0, 0, 0, 0],
        "speed": [0.0] * 6,
    }).to_parquet(old_meta / "phase_signals.parquet", index=False)
    (old_meta / "piper_cameras.json").write_text('{"top": "rs_x"}')
    return ds


def test_remap_after_delete(edited_dataset):
    moved = remap_after_delete(edited_dataset, [1, 3])
    assert set(moved) == {"phase_labels.json", "phase_signals.parquet", "piper_cameras.json"}

    labels = json.loads((edited_dataset / "meta" / "phase_labels.json").read_text())
    # 옛 0,2,4 → 새 0,1,2. cycles 값(옛 번호와 같음)으로 어느 에피소드였는지 추적한다
    assert set(labels["episodes"]) == {"0", "1", "2"}
    assert labels["episodes"]["1"]["cycles"] == 2   # 옛 #2
    assert labels["episodes"]["2"]["cycles"] == 4   # 옛 #4 — 밀렸다면 여기가 3 이 된다

    sig = pd.read_parquet(edited_dataset / "meta" / "phase_signals.parquet")
    assert sorted(sig["episode_index"].unique()) == [0, 1, 2]
    assert len(sig[sig.episode_index == 0]) == 2    # 옛 #0 의 2프레임 유지

    assert (edited_dataset / "meta" / "piper_cameras.json").read_text() == '{"top": "rs_x"}'


def test_remap_without_sidecar_is_quiet(edited_dataset):
    for f in ("phase_labels.json", "phase_signals.parquet", "piper_cameras.json"):
        (edited_dataset.with_name("myset_old") / "meta" / f).unlink()
    assert remap_after_delete(edited_dataset, [0]) == []


def test_remap_without_backup_is_quiet(tmp_path):
    ds = tmp_path / "noback"
    (ds / "meta").mkdir(parents=True)
    assert remap_after_delete(ds, [0]) == []


def test_edit_args_route_through_wrapper():
    args = build_edit_dataset_args("u/ds", "delete_episodes", {"episode_indices": "[1,3]"})
    # 래퍼를 거쳐야 사이드카 동기화가 편집과 같은 프로세스에서 따라붙는다
    assert args[0] == cli_mapping.settings.grpc_python
    assert args[2].endswith("wrapper/edit_dataset.py")
    # lerobot 0.5 는 밑줄 플래그다 — `--repo-id` 는 CLI 가 거부한다 (실측)
    assert "--repo_id=u/ds" in args
    assert "--operation.type=delete_episodes" in args
    assert "--operation.episode_indices=[1,3]" in args
