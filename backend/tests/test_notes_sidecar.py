"""이름·설명 사이드카 — LeRobot 구조에 없는 자리를 옆에 만든다.

`meta/info.json`(데이터셋)·`config.json`(체크포인트)에는 사람이 붙이는
이름·설명 자리가 없고, 거기 임의 키를 끼우면 LeRobot 도구가 재작성할 때
보존된다는 보장이 없다. 그래서 별도 파일이다 — `meta/piper_cameras.json`
(카메라 매핑)과 같은, 이 저장소의 확립된 사이드카 관례.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import notes_sidecar as ns


def _dataset(root: Path) -> Path:
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text("{}")
    return root


def test_roundtrip_for_datasets_and_models(tmp_path):
    ds = _dataset(tmp_path / "ds")
    ds.parent.mkdir(exist_ok=True)
    out = ns.write_notes(ds, kind="dataset", name="볼트 1차", description="주간 조명")
    assert out["updated_at"]
    got = ns.read_notes(ds, kind="dataset")
    assert got["name"] == "볼트 1차" and got["description"] == "주간 조명"
    # 파일 자리 자체가 계약이다 — LeRobot 이 안 건드리는 meta/ 아래
    assert (ds / "meta" / "piper_notes.json").exists()

    model = tmp_path / "ckpt"
    model.mkdir()
    ns.write_notes(model, kind="model", name="", description="lr 낮춘 재학습")
    assert ns.read_notes(model, kind="model")["description"] == "lr 낮춘 재학습"


def test_missing_or_broken_sidecar_reads_as_empty(tmp_path):
    """목록 스캔이 데이터셋마다 부른다 — 없거나 깨졌다고 시끄러우면 안 된다."""
    assert ns.read_notes(tmp_path, kind="dataset") == ns.EMPTY
    _dataset(tmp_path)
    (tmp_path / "meta" / "piper_notes.json").write_text("{깨진 json")
    assert ns.read_notes(tmp_path, kind="dataset")["name"] == ""


def test_dataset_write_refuses_where_there_is_no_meta(tmp_path):
    """meta/ 없는 곳에 만들면 데이터셋이 아닌 디렉토리에 흔적이 남는다
    (camera_sidecar 와 같은 규칙)."""
    with pytest.raises(FileNotFoundError):
        ns.write_notes(tmp_path, kind="dataset", name="x", description="")


def test_readme_is_created_once_and_never_clobbered(tmp_path):
    """업로드가 폴더 전체를 올리므로 README.md 가 곧 허브 카드다.
    ⚠ 이미 있으면 안 덮는다 — 사람이 다듬은 카드를 업로드가 지우면 안 된다."""
    _dataset(tmp_path)
    notes = ns.write_notes(tmp_path, kind="dataset", name="볼트 1차", description="주간")
    assert ns.ensure_readme(tmp_path, "org/ds", notes) is True
    text = (tmp_path / "README.md").read_text()
    assert "# 볼트 1차" in text and "주간" in text

    (tmp_path / "README.md").write_text("사람이 고친 카드")
    assert ns.ensure_readme(tmp_path, "org/ds", notes) is False
    assert (tmp_path / "README.md").read_text() == "사람이 고친 카드"

    # 이름도 설명도 없으면 빈 카드를 만들지 않는다
    empty = tmp_path / "empty"
    _dataset(empty)
    assert ns.ensure_readme(empty, "org/empty", ns.EMPTY) is False


def test_the_scan_carries_notes_so_lists_can_show_them(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.dataset_scanner import scan_datasets

    root = tmp_path / "lerobot" / "org" / "ds1"
    root.mkdir(parents=True)
    (root / "meta").mkdir()
    (root / "meta" / "info.json").write_text(json.dumps({"total_episodes": 3}))
    ns.write_notes(root, kind="dataset", name="이름", description="설명")
    monkeypatch.setattr(settings, "lerobot_dir", tmp_path / "lerobot")
    monkeypatch.setattr(settings, "datasets_dir", tmp_path / "none")
    found = [d for d in scan_datasets() if d["id"] == "org/ds1"]
    assert found and found[0]["notes"]["name"] == "이름"


def test_notes_api_reads_and_writes(tmp_path, monkeypatch):
    from app.main import app
    from app.routers import datasets as ds_router

    root = _dataset(tmp_path)
    monkeypatch.setattr(ds_router, "find_dataset_path", lambda _id: root)
    c = TestClient(app)
    r = c.put("/api/datasets/org/ds/notes", json={"name": "n", "description": "d"})
    assert r.status_code == 200
    assert c.get("/api/datasets/org/ds/notes").json()["name"] == "n"


def test_recording_carries_the_description_to_the_sidecar():
    """녹화 폼의 설명이 정지 시 사이드카가 된다 — 카메라 사이드카와 같은 시점.
    빈 값이면 안 쓴다: 기존 설명을 빈 값으로 덮는 사고 방지."""
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "recording.py").read_text()
    assert "description: str" in src
    stop = src.split("write_camera_sidecar(", 1)[1]
    assert "write_notes" in stop and "if repo_id and desc" in stop


def test_upload_makes_the_card_before_pushing():
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "datasets.py").read_text()
    body = src.split("async def upload_to_hub", 1)[1].split("async def", 1)[0]
    assert "ensure_readme" in body, "업로드가 카드를 안 만든다 — 허브에 설명이 안 간다"
