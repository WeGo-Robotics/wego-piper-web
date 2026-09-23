"""허브에서 받은 데이터셋의 영상 서빙.

⚠ 이 파일이 지키는 것은 하나다: **경로 가드가 심볼릭을 따라가면 안 된다.**

HF 캐시는 `snapshots/<hash>/videos/…/file-000.mp4` 를 `blobs/<sha>` 로 **링크**한다.
`.resolve()` 로 "데이터셋 안인가" 를 판정하면 그 링크가 스냅샷 밖(`blobs/`)으로 나가므로
**허브에서 받은 데이터셋은 통째로 404** 가 된다. 화면에서는 코덱 문제처럼 보인다 —
브라우저가 mp4 대신 404 JSON 을 받아 `Format error` 를 내기 때문이다
(실기 2026-09-23, `wego-mink/sim_two_box_3_120`).

막아야 하는 것은 템플릿 안의 `..` 이지 데이터셋 안의 심볼릭이 아니다.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

KEY = "observation.images.cam0"


def _hf_shaped(tmp_path):
    """HF 캐시와 같은 모양 — 실체는 `blobs/`, 스냅샷은 링크."""
    root = tmp_path / "datasets--org--name"
    blob = root / "blobs" / "deadbeef"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 64)

    snap = root / "snapshots" / "abc123"
    vid_dir = snap / "videos" / KEY / "chunk-000"
    vid_dir.mkdir(parents=True)
    (vid_dir / "file-000.mp4").symlink_to(blob)

    (snap / "meta").mkdir()
    (snap / "meta" / "info.json").write_text(json.dumps({
        "codebase_version": "v3.0",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
    }))
    return snap


@pytest.fixture
def served(tmp_path, monkeypatch):
    snap = _hf_shaped(tmp_path)
    monkeypatch.setattr("app.routers.datasets.find_dataset_path", lambda _id: snap)
    return TestClient(app), snap


def test_a_symlinked_video_is_served(served):
    """⚠ **이게 요점이다.** 허브에서 받은 것은 전부 이 모양이다."""
    c, _ = served
    r = c.get("/api/datasets/org/name/videos/cam0/0/0")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "video/mp4"
    assert r.content.startswith(b"\x00\x00\x00\x18ftyp")


def test_a_dangling_symlink_is_still_a_404(served):
    """받다 만 데이터셋 — 링크는 있는데 실체가 없다. 있다고 말하면 안 된다."""
    c, snap = served
    (snap.parents[1] / "blobs" / "deadbeef").unlink()   # 실체만 지운다 — 링크는 남는다
    assert c.get("/api/datasets/org/name/videos/cam0/0/0").status_code == 404


def test_the_guard_still_refuses_climbing_out(served):
    """⚠ 원래 막으려던 것은 이것이다 — 심볼릭을 허용한다고 `..` 까지 열면 안 된다."""
    c, snap = served
    (snap.parents[2] / "secret.mp4").write_bytes(b"nope")
    evil = "..%2F..%2F..%2Fsecret"
    assert c.get(f"/api/datasets/org/name/videos/{evil}/0/0").status_code == 404


def test_the_template_comes_from_the_dataset_not_from_us(served):
    """v2.1 은 `videos/chunk-000/<키>/episode_000000.mp4` 다. 우리가 한 모양을 박으면
    한쪽 버전이 통째로 안 보인다 — `meta/info.json` 이 답을 들고 있다."""
    c, snap = served
    alt = snap / "videos" / "chunk-000" / KEY
    alt.mkdir(parents=True)
    (alt / "episode_000000.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42alt")
    info = json.loads((snap / "meta" / "info.json").read_text())
    info["video_path"] = "videos/chunk-{chunk_index:03d}/{video_key}/episode_{file_index:06d}.mp4"
    (snap / "meta" / "info.json").write_text(json.dumps(info))
    r = c.get("/api/datasets/org/name/videos/cam0/0/0")
    assert r.status_code == 200 and r.content.endswith(b"alt")
