"""에피소드 뷰어 1단계 — 프레임 서빙 + 페이즈 요약 (feature/episode-editor.md §3~4).

핵심 회귀는 **라우트 순서**다: `GET /{dataset_id:path}` (상세) 가 catch-all 이라
프레임 GET 이 그 아래 등록되면 경로 전체가 상세 응답으로 먹힌다.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.dataset_scanner import find_dataset_path

_DS = "wego-hansu/min_cube_071410"


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def real_ds(client):
    if find_dataset_path(_DS) is None:
        pytest.skip(f"{_DS} 없음")
    return _DS


# ── 프레임 서빙 ──

def test_frame_unknown_dataset_404(client):
    r = client.get("/api/datasets/no/such-ds/episodes/0/frames/top/0")
    assert r.status_code == 404


def test_frame_route_not_shadowed_by_detail(client, real_ds):
    """실데이터셋의 프레임 경로가 상세(catch-all) JSON 으로 먹히면 안 된다.

    캐시 유무와 무관하게 성립해야 한다: 캐시가 있으면 이미지, 없으면
    캐시 안내 404 — 어느 쪽이든 상세 응답(JSON dict + id 필드)은 아니다.
    """
    r = client.get(f"/api/datasets/{real_ds}/episodes/0/frames/top/0")
    if r.status_code == 200:
        assert r.headers["content-type"].startswith("image/")
    else:
        assert r.status_code == 404
        assert "디코딩 캐시" in r.json()["detail"]


def test_frame_serves_cached_image(client, real_ds):
    """캐시가 있을 때만: jpg/png 를 실제 이미지로 서빙 + 불변 캐시 헤더."""
    ds_path = find_dataset_path(real_ds)
    ep_dir = ds_path / "images"
    cams = [d.name.removeprefix("observation.images.") for d in ep_dir.iterdir()] if ep_dir.is_dir() else []
    if not cams:
        pytest.skip("디코딩 캐시 없음")
    r = client.get(f"/api/datasets/{real_ds}/episodes/0/frames/{cams[0]}/0")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert "immutable" in r.headers.get("cache-control", "")


def test_decode_cache_rejects_unknown_format(client):
    r = client.post("/api/datasets/no/such-ds/decode-cache", json={"format": "webp"})
    assert r.status_code == 422


def test_decode_cache_unknown_dataset_404_before_start(client):
    """모르는 데이터셋이면 프로세스를 시작하기 전에 404 로 끊어야 한다."""
    r = client.post("/api/datasets/no/such-ds/decode-cache", json={"format": "jpeg", "max_dim": 320})
    assert r.status_code == 404


# ── 비디오 서빙 (뷰어 기본 모드) ──

def test_video_unknown_dataset_404(client):
    assert client.get("/api/datasets/no/such-ds/videos/top/0/0").status_code == 404


def test_video_serves_mp4_with_range(client, real_ds):
    """비디오 모드의 전제 둘: mp4 로 열리고, Range 가 206 으로 온다 (탐색용)."""
    url = f"/api/datasets/{real_ds}/videos/top/0/0"
    r = client.get(url, headers={"Range": "bytes=0-99"})
    if r.status_code == 404:
        pytest.skip("비디오 파일 없음")
    assert r.status_code == 206
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers.get("content-range", "").startswith("bytes 0-99/")
    assert len(r.content) == 100


def test_video_route_not_shadowed_by_detail(client, real_ds):
    r = client.get(f"/api/datasets/{real_ds}/videos/top/0/0", headers={"Range": "bytes=0-0"})
    assert r.status_code in (206, 404)
    if r.status_code == 206:
        assert not r.headers["content-type"].startswith("application/json")


# ── 페이즈 요약 (⚠ 배지) ──

def test_phase_summary_shape(client, real_ds):
    r = client.get(f"/api/phase/{real_ds}/summary")
    if r.status_code == 404:
        pytest.skip("사이드카 없음")
    body = r.json()
    assert set(body) >= {"episodes", "cycle_distribution", "median_cycles", "outliers"}
    for f in body["outliers"]:
        assert set(f) >= {"episode", "cycles", "reasons"}


# ── 디코딩 스크립트 ──

def test_decode_script_episode_lengths(real_ds):
    """스크립트의 에피소드 길이 로딩이 메타와 일치해야 한다 (멀티 chunk 수정의 전제)."""
    pytest.importorskip("cv2")
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "decode_cache.py"
    spec = importlib.util.spec_from_file_location("decode_cache", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ds_path = find_dataset_path(_DS)
    lengths = mod.episode_lengths(ds_path)
    import json
    info = json.loads((ds_path / "meta" / "info.json").read_text())
    assert len(lengths) == info["total_episodes"]
    assert sum(lengths.values()) == info["total_frames"]
