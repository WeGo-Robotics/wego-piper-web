"""외부 제어 API — 인증 계약과 미션 표면 (feature/external-api.md).

핵심은 **기본 잠김**이다: 토큰 미설정이면 503, 틀리면 401.
미션은 오케스트레이터 위의 얇은 표면이라 seam 은 오케스트레이터 자체를 흉내낸다.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.routers import external as ext_mod
from app.services.orchestrator import orchestrator

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def authed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "api_token", TOKEN)
    monkeypatch.setattr(settings, "log_dir", tmp_path)
    return tmp_path


# ── 인증 — 기본 잠김 ──


def test_no_token_configured_is_503(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "")
    r = client.get("/api/ext/v1/status", headers=AUTH)
    assert r.status_code == 503


def test_wrong_token_is_401(client, authed):
    r = client.get("/api/ext/v1/status", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert client.get("/api/ext/v1/status").status_code == 401  # 헤더 없음


def test_auth_covers_every_route(client, monkeypatch):
    """새 엔드포인트를 추가하며 인증을 빠뜨리는 실수를 구조적으로 막는다."""
    monkeypatch.setattr(settings, "api_token", TOKEN)
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/ext/"):
            continue
        concrete = path.replace("{mission_id}", "run_20260101_000000")
        for method in route.methods - {"HEAD", "OPTIONS"}:
            r = client.request(method, concrete)  # 토큰 없이
            assert r.status_code == 401, f"{method} {path} 가 인증 없이 {r.status_code}"


# ── 미션 ──


def test_start_mission_maps_to_orchestrator(client, authed, monkeypatch):
    started = {}

    async def fake_start(cfg):
        started["cfg"] = cfg
        orchestrator.state = "running"
        orchestrator.journal_path = authed / "orchestrator" / "run_20260816_120000.jsonl"

    monkeypatch.setattr(orchestrator, "start", fake_start)
    monkeypatch.setattr(orchestrator, "state", "idle")
    r = client.post("/api/ext/v1/missions", headers=AUTH,
                    json={"type": "recycling", "max_episodes": 5, "dry_run": True})
    assert r.status_code == 200
    assert r.json()["id"] == "run_20260816_120000"
    assert started["cfg"].max_episodes == 5 and started["cfg"].dry_run is True
    orchestrator.state = "idle"
    orchestrator.journal_path = None


def test_unknown_mission_type_is_400(client, authed):
    r = client.post("/api/ext/v1/missions", headers=AUTH, json={"type": "popcorn"})
    assert r.status_code == 400


def test_orchestrator_refusal_is_409(client, authed, monkeypatch):
    async def fake_start(cfg):
        raise RuntimeError("추론이 실행 중이 아닙니다")

    monkeypatch.setattr(orchestrator, "start", fake_start)
    r = client.post("/api/ext/v1/missions", headers=AUTH, json={"type": "recycling"})
    assert r.status_code == 409


def test_finished_mission_reads_journal(client, authed):
    jdir = authed / "orchestrator"
    jdir.mkdir()
    lines = [{"event": "run_start"}, {"event": "episode", "outcome": "timeout_done"}]
    (jdir / "run_20260816_010101.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + '\n{"잘린'
    )
    r = client.get("/api/ext/v1/missions/run_20260816_010101", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["live"] is False
    assert body["events"] == lines  # 잘린 마지막 줄은 버린다

    listing = client.get("/api/ext/v1/missions", headers=AUTH).json()["missions"]
    assert [m["id"] for m in listing] == ["run_20260816_010101"]


def test_bad_mission_id_is_rejected(client, authed):
    # 인코딩된 경로조작은 라우팅 단계에서 404 — 핸들러까지 못 온다
    r = client.get("/api/ext/v1/missions/..%2F..%2Fetc", headers=AUTH)
    assert r.status_code in (400, 404)
    # 형식이 다른 id 는 핸들러의 정규식 검증이 400
    r = client.get("/api/ext/v1/missions/run_x", headers=AUTH)
    assert r.status_code == 400


def test_missing_mission_is_404(client, authed):
    r = client.get("/api/ext/v1/missions/run_20990101_000000", headers=AUTH)
    assert r.status_code == 404


def test_cancel_only_hits_live_mission(client, authed):
    r = client.post("/api/ext/v1/missions/run_20990101_000000/cancel", headers=AUTH)
    assert r.status_code == 409


# ── 상태·안전 ──


def test_status_shape(client, authed):
    r = client.get("/api/ext/v1/status", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    for key in ("activities", "estop", "orchestrator", "vision"):
        assert key in body


def test_heartbeat_reaches_bridge(client, authed, monkeypatch):
    beats = []
    monkeypatch.setattr(ext_mod.estop_bridge, "heartbeat", lambda: beats.append(1))
    r = client.post("/api/ext/v1/heartbeat", headers=AUTH)
    assert r.status_code == 200 and beats == [1]


def test_estop_reaches_bridge(client, authed, monkeypatch):
    async def fake_trigger():
        return ["inference"]

    monkeypatch.setattr(ext_mod.estop_bridge, "trigger_manual", fake_trigger)
    r = client.post("/api/ext/v1/estop", headers=AUTH)
    assert r.status_code == 200 and r.json()["stopped"] == ["inference"]
