"""옛 `yolo_*` 데이터 디렉토리 → `detector_*` 이관 (feature/plugins.md §14-2).

⚠ **삭제 예정 — 한 릴리스 뒤**, `legacy_paths.py` 와 함께. 옛 이름을 적는 것이
허용된 파일이다 (`test_no_model_branding.py` 허용 목록).
"""

from pathlib import Path

from app.services.legacy_paths import migrate_legacy_dirs


def _dirs(root: Path) -> dict[str, Path]:
    return {"detector_models": root / "detector_models",
            "detector_datasets": root / "detector_datasets"}


def test_old_only_is_renamed(tmp_path: Path):
    old = tmp_path / "yolo_models"
    (old / "a").mkdir(parents=True)
    (old / "a" / "w.bin").write_bytes(b"x")

    moved = migrate_legacy_dirs(_dirs(tmp_path))

    assert not old.exists()
    assert (tmp_path / "detector_models" / "a" / "w.bin").read_bytes() == b"x"
    assert len(moved) == 1


def test_placeholder_new_dir_receives_the_contents(tmp_path: Path):
    """소스 체크아웃·새 이미지는 `.gitkeep` 만 든 새 디렉토리를 이미 갖고 있다."""
    old = tmp_path / "yolo_datasets"
    (old / "TEST").mkdir(parents=True)
    (old / "_training.json").write_text("{}")
    new = tmp_path / "detector_datasets"
    new.mkdir()
    (new / ".gitkeep").touch()

    migrate_legacy_dirs(_dirs(tmp_path))

    assert not old.exists()
    assert (new / "TEST").is_dir() and (new / "_training.json").exists()
    assert (new / ".gitkeep").exists()


def test_both_populated_is_left_alone(tmp_path: Path):
    old = tmp_path / "yolo_models"
    (old / "x.bin").parent.mkdir()
    (old / "x.bin").write_bytes(b"1")
    new = tmp_path / "detector_models"
    new.mkdir()
    (new / "y.bin").write_bytes(b"2")

    moved = migrate_legacy_dirs(_dirs(tmp_path))

    assert moved == []
    assert (old / "x.bin").exists() and (new / "y.bin").exists()


def test_nothing_to_do_is_quiet(tmp_path: Path):
    assert migrate_legacy_dirs(_dirs(tmp_path)) == []
