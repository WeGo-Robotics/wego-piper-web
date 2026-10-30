"""영상 Range 응답은 짧게 끝난다 — 크롬의 호스트당 연결 6개를 영상이 쥐고 있지 않게.

`<video>` 는 `bytes=0-`(끝까지)로 받다가 버퍼가 차면 읽기만 멈추고 연결을 쥐고 있는다. 카메라 3대
뷰어가 두 개 열리자 6자리가 다 차서 화면의 모든 API 가 30초 시간초과로 죽었다(실기 2026-10-29).
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

KEY = "observation.images.cam0"
BODY = bytes(range(256)) * 8          # 2048 바이트
CAP = 100


@pytest.fixture
def client(tmp_path, monkeypatch):
    vid = tmp_path / "videos" / KEY / "chunk-000"
    vid.mkdir(parents=True)
    (vid / "file-000.mp4").write_bytes(BODY)
    (tmp_path / "meta").mkdir()
    (tmp_path / "meta" / "info.json").write_text(json.dumps({"codebase_version": "v3.0"}))
    monkeypatch.setattr("app.routers.datasets.find_dataset_path", lambda _id: tmp_path)
    monkeypatch.setattr("app.routers.datasets.VIDEO_RANGE_CAP", CAP)
    return TestClient(app)


URL = "/api/datasets/org/name/videos/cam0/0/0?v=1"


def test_an_open_ended_range_gets_one_chunk(client):
    """⚠ 요점. 브라우저 영상은 늘 `bytes=N-` 로 묻는다."""
    r = client.get(URL, headers={"Range": "bytes=0-"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-{CAP - 1}/{len(BODY)}"
    assert r.content == BODY[:CAP]

    r = client.get(URL, headers={"Range": "bytes=2000-"})
    assert r.headers["content-range"] == f"bytes 2000-2047/{len(BODY)}", "끝 조각은 파일 끝까지"
    assert r.content == BODY[2000:]


def test_a_small_or_suffix_range_is_untouched(client):
    r = client.get(URL, headers={"Range": "bytes=10-19"})
    assert r.headers["content-range"] == f"bytes 10-19/{len(BODY)}" and r.content == BODY[10:20]
    r = client.get(URL, headers={"Range": "bytes=-30"})
    assert r.content == BODY[-30:]


def test_without_a_range_the_whole_file_comes(client):
    """다운로드 같은 일반 요청은 그대로 200 통째로."""
    r = client.get(URL)
    assert r.status_code == 200 and r.content == BODY
