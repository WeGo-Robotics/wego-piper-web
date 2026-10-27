"""데이터셋 썸네일 (services/dataset_thumbnail, `GET /api/datasets/{id}/thumbnail`).

고르는 창이 데이터셋을 그림으로 보여준다. 브라우저에 mp4 수십 개를 열게 하지 않고
게이트웨이가 작은 JPEG 한 장을 만들어 캐시한다.
"""

import json
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")


def _dataset(root: Path, *, cams=("hand", "top"), video=True) -> Path:
    features = {f"observation.images.{c}": {"dtype": "video", "shape": [48, 64, 3]} for c in cams}
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text(json.dumps(
        {"fps": 10, "total_episodes": 1, "total_frames": 20, "features": features}))
    if video:
        for c in cams:
            out = root / "videos" / f"observation.images.{c}" / "chunk-000" / "file-000.mp4"
            out.parent.mkdir(parents=True)
            w = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
            for i in range(20):
                w.write(np.full((48, 64, 3), 200 if c == "top" else 30, np.uint8))
            w.release()
    return root


@pytest.fixture
def cache(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "log_dir", tmp_path / "data")
    return tmp_path / "data" / "thumbnails"


def test_a_thumbnail_is_made_once_from_the_top_camera(tmp_path, cache):
    """위에서 내려다보는 카메라가 장면을 가장 잘 보여준다. 두 번째부터는 캐시다."""
    from app.services.dataset_thumbnail import thumbnail

    ds = _dataset(tmp_path / "ds")
    out = thumbnail(ds)
    assert out is not None and out.parent == cache and out.suffix == ".jpg"
    img = cv2.imread(str(out))
    assert img.mean() > 150, "top 이 아니라 hand 카메라에서 뽑았다"
    stamp = out.stat().st_mtime_ns
    assert thumbnail(ds) == out and out.stat().st_mtime_ns == stamp, "캐시가 있는데 다시 만들었다"
    assert not list(ds.rglob("*.jpg")), "데이터셋 폴더에 썼다 — meta/ 는 허브 업로드에 동반된다"


def test_no_video_means_no_thumbnail(tmp_path, cache):
    from app.services.dataset_thumbnail import thumbnail

    assert thumbnail(_dataset(tmp_path / "ds", video=False)) is None
    assert thumbnail(tmp_path / "nothing") is None


def test_the_route_sits_above_the_detail_catch_all(tmp_path, cache, monkeypatch):
    """⚠ `GET /{dataset_id:path}` 가 먼저 있으면 `…/thumbnail` 이 상세 응답으로 먹힌다."""
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app

    _dataset(tmp_path / "lerobot" / "org" / "pick")
    monkeypatch.setattr(settings, "lerobot_dir", tmp_path / "lerobot")
    monkeypatch.setattr(settings, "datasets_dir", tmp_path / "none")
    r = TestClient(app).get("/api/datasets/org/pick/thumbnail")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
