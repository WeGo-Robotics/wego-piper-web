"""데이터셋 [업로드] 가 올라가는 곳 — 접속된 계정 아래 `<계정>/<이름>`.

로컬 id 의 앞부분(`wego-hansu/dualarm_sort_apple`)은 녹화한 사람의 폴더 이름이다. 토큰이 다른
계정(`wego-mink`)이면 그 이름으로는 올릴 수 없었다 — 올라가는 곳을 접속된 계정으로 정한다.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.services import hub_client

REPO = Path(__file__).resolve().parents[2]


def test_repo_under_drops_the_local_namespace():
    assert hub_client.repo_under("wego-mink", "wego-hansu/dualarm_sort_apple") == "wego-mink/dualarm_sort_apple"
    assert hub_client.repo_under("wego-mink", "act-apple") == "wego-mink/act-apple"
    assert hub_client.repo_under("wego-mink", " x ") == "wego-mink/x"


def test_repo_under_refuses_when_the_account_is_unknown():
    with pytest.raises(HTTPException) as e:
        hub_client.repo_under("", "wego-hansu/ds")
    assert e.value.status_code == 400


@pytest.fixture
def env(tmp_path, monkeypatch):
    import app.routers.datasets as D

    ds = tmp_path / "wego-hansu" / "dualarm_sort_apple"
    (ds / "meta").mkdir(parents=True)
    started: list[list[str]] = []

    async def fake_start(args, *a, **k):
        started.append(list(args))

    async def connected():
        return "wego-mink"

    monkeypatch.setattr(D, "find_dataset_path", lambda i: ds if i == "wego-hansu/dualarm_sort_apple" else None)
    monkeypatch.setattr(D._upload_pm, "start", fake_start)
    monkeypatch.setattr(D, "_find_hf_cli", lambda: "/usr/bin/hf")
    monkeypatch.setattr(hub_client, "require_hub_write", connected)
    from app.main import app
    return TestClient(app), started


def test_the_dataset_goes_under_the_connected_account(env):
    c, started = env
    r = c.post("/api/datasets/wego-hansu/dualarm_sort_apple/upload", json={})
    assert r.status_code == 200, r.text
    assert r.json()["repo_id"] == "wego-mink/dualarm_sort_apple"
    [args] = started
    # 스크립트의 첫 인자 = 올라가는 저장소. 로컬 id 가 그대로 가면 남의 이름공간이다
    assert "wego-mink/dualarm_sort_apple" in args and "wego-hansu/dualarm_sort_apple" not in args


def test_an_unreachable_hub_does_not_guess_the_account(env, monkeypatch):
    c, started = env

    async def unreachable():
        return ""

    monkeypatch.setattr(hub_client, "require_hub_write", unreachable)
    r = c.post("/api/datasets/wego-hansu/dualarm_sort_apple/upload", json={})
    assert r.status_code == 400
    assert started == []


def test_the_page_shows_the_target_before_asking():
    """누르기 전에 올라갈 곳을 보여 준다 — 로컬 id 와 다를 수 있다."""
    page = (REPO / "frontend" / "src" / "pages" / "DatasetsPage.tsx").read_text()
    assert "/hub/whoami" in page.split("const handleUpload", 1)[1].split("const handleDelta", 1)[0]
