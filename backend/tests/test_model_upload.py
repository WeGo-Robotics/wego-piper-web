"""체크포인트를 Hub 모델 저장소로 올린다 (`POST /api/models/{id}/upload`).

지금까지 모델이 Hub 로 가는 길은 학습 폼의 Policy Repo ID(학습 끝에 LeRobot 이 올린다) 하나뿐이라,
이미 끝난 학습의 체크포인트는 올릴 수 없었다. 데이터셋 업로드와 같은 작업 유닛·같은 로그 창을 쓴다.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services import hub_client
from app.services import notes_sidecar as ns

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """학습 산출물 하나 + 가짜 업로드 유닛. 실제로 올리지 않는다."""
    import app.routers.datasets as D

    monkeypatch.setattr(Settings, "model_paths", property(lambda self: [tmp_path]))
    run = tmp_path / "2026-10-27" / "11-43-50_act"
    pm = run / "checkpoints" / "020000" / "pretrained_model"
    pm.mkdir(parents=True)
    (pm / "config.json").write_text(json.dumps({"type": "act"}))
    ns.write_notes(run, kind="model", name="사과 분류", description="주간 조명")

    started: list[list[str]] = []

    async def fake_start(args, *a, **k):
        started.append(list(args))

    async def connected():
        return "wego-mink"

    monkeypatch.setattr(D._upload_pm, "start", fake_start)
    monkeypatch.setattr(D, "_find_hf_cli", lambda: "/usr/bin/hf")
    monkeypatch.setattr(hub_client, "require_hub_write", connected)
    from app.main import app
    return TestClient(app), pm, started


def test_a_checkpoint_folder_goes_to_the_named_model_repo(env):
    c, pm, started = env
    r = c.post("/api/models/2026-10-27/11-43-50_act/020000/upload", json={"repo_id": "wego/act-apple", "private": True})
    assert r.status_code == 200, r.text
    [args] = started
    assert args[:5] == ["/usr/bin/hf", "upload", "wego/act-apple", str(pm), "."], "체크포인트 폴더를 그 저장소로 안 올린다"
    assert "--repo-type=model" in args and "--private" in args
    # 학습 이름·설명이 허브 카드가 된다 (체크포인트는 run 의 이름을 물려받는다)
    assert "사과 분류" in (pm / "README.md").read_text()


def test_a_bare_name_goes_under_the_connected_account(env):
    """⚠ 화면은 이름만 보낸다. 계정을 사람이 적게 두면 토큰의 계정과 어긋나 거절당했다
    (`wego-mink/…` 를 올리려는데 접속된 계정이 달랐다)."""
    c, pm, started = env
    r = c.post("/api/models/2026-10-27/11-43-50_act/020000/upload", json={"repo_id": "act-apple"})
    assert r.status_code == 200, r.text
    assert r.json()["repo_id"] == "wego-mink/act-apple"
    [args] = started
    assert args[:5] == ["/usr/bin/hf", "upload", "wego-mink/act-apple", str(pm), "."]


def test_an_explicit_namespace_is_kept_for_api_callers(env):
    """조직으로 올리고 싶은 외부 호출은 `네임스페이스/이름` 을 직접 준다 — 그대로 간다."""
    c, _, started = env
    r = c.post("/api/models/2026-10-27/11-43-50_act/020000/upload", json={"repo_id": "wego-robotics/act-apple"})
    assert r.status_code == 200, r.text
    assert started[0][2] == "wego-robotics/act-apple"


def test_a_bare_name_needs_to_know_the_account(env, monkeypatch):
    """HF 가 안 닿으면 계정을 모른다 — 엉뚱한 곳으로 올리지 않고 거절한다."""
    c, _, started = env

    async def unreachable():
        return ""

    monkeypatch.setattr(hub_client, "require_hub_write", unreachable)
    r = c.post("/api/models/2026-10-27/11-43-50_act/020000/upload", json={"repo_id": "act-apple"})
    assert r.status_code == 400 and "계정" in r.text
    assert started == []


def test_the_repo_name_must_be_a_plain_name(env):
    """모델 id 는 학습 경로라 그대로 저장소 이름이 될 수 없다 — 사람이 정한 이름만 받는다."""
    c, _, started = env
    for bad in ("wego/", "a b/c", "a b", "2026-10-27/11-43-50_act/020000", "a/b/c"):
        r = c.post("/api/models/2026-10-27/11-43-50_act/020000/upload", json={"repo_id": bad})
        assert r.status_code == 400, bad
    assert c.post("/api/models/nope/x/upload", json={"repo_id": "x"}).status_code == 404
    assert started == [], "막아야 할 요청이 업로드를 시작했다"


def test_the_storage_window_and_table_offer_the_upload():
    """저장소의 모델 창(체크포인트 상세)과 표의 행에서 올린다 — 데이터셋 화면과 같다."""
    page = (REPO / "frontend" / "src" / "pages" / "ModelsPage.tsx").read_text()
    assert "/upload`, {" in page and "repo_id" in page
    assert "actions={(row) =>" in page, "표의 행에서 올릴 수 없다"
    # 계정은 사람이 적지 않는다 — 접속된 계정이 앞에 고정되고 이름 칸만 있다
    assert "RepoIdInput" not in page, "모델 업로드 창이 계정을 고르게 한다"
    assert "onUpload=" in page, "모델 창에서 올릴 수 없다"
    # 진행은 데이터셋 업로드와 같은 유닛 — 같은 WS 메시지·같은 중지
    assert "'upload_log'" in page and "/datasets/upload-stop" in page
