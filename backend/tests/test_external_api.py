"""외부 제어 API — 인증 계약과 미션 표면 (feature/external-api.md, feature/plugins.md §12-2).

핵심은 **기본 잠김**이다: 토큰 미설정이면 503, 틀리면 401.
미션은 플러그인 위의 얇은 표면이라 seam 은 플러그인 레지스트리다 — 프로세스는 띄우지 않는다.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.routers import external as ext_mod
from app.services import plugins as P
from app.services.plugins import registry

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

MISSION_YAML = "name: recycling\nmission: true\nconfig:\n  - { key: max_episodes, kind: number, default: 10 }\n"


class FakePM:
    def __init__(self):
        from app.services.process_manager import ProcessState
        self.state, self.pid, self.cmd, self.env = ProcessState.IDLE, None, None, None

    def set_state_callback(self, cb):
        pass

    @property
    def is_running(self):
        from app.services.process_manager import ProcessState
        return self.state in (ProcessState.RUNNING, ProcessState.STARTING)

    def set_log_callback(self, cb):
        pass

    async def start(self, cmd, env_extra=None):
        from app.services.process_manager import ProcessState
        self.cmd, self.env, self.state, self.pid = cmd, env_extra, ProcessState.RUNNING, 777

    async def stop(self):
        from app.services.process_manager import ProcessState
        self.state, self.pid = ProcessState.IDLE, None

    async def kill(self):
        await self.stop()

    def recent_log(self, n=20):
        return []


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def authed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "api_token", TOKEN)
    monkeypatch.setattr(settings, "plugins_dir", tmp_path / "plugins")
    monkeypatch.setattr(P, "BUNDLED_DIR", tmp_path / "bundled")
    (tmp_path / "plugins").mkdir()
    fakes = {}
    monkeypatch.setattr("app.services.systemd_process.make_process", lambda unit: fakes.setdefault(unit, FakePM()))
    monkeypatch.setattr("app.services.estop_bridge.estop_bridge.sync_activities", lambda: None)
    registry._owners.clear(); registry._runs.clear(); registry._grants.clear(); registry._seen.clear()
    yield tmp_path
    registry._owners.clear(); registry._runs.clear(); registry._grants.clear(); registry._seen.clear()


def _install_mission(root, name="recycling", mission=True):
    d = root / "plugins" / name
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(MISSION_YAML.replace("recycling", name).replace("mission: true", f"mission: {str(mission).lower()}"))
    (d / "main.py").write_text("")
    return d


# ── 인증 — 기본 잠김 ──


def test_no_token_configured_is_503(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "")
    assert client.get("/api/ext/v1/status", headers=AUTH).status_code == 503


def test_wrong_token_is_401(client, authed):
    assert client.get("/api/ext/v1/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/ext/v1/status").status_code == 401  # 헤더 없음


def test_auth_covers_every_route(client, monkeypatch):
    """새 엔드포인트를 추가하며 인증을 빠뜨리는 실수를 구조적으로 막는다."""
    monkeypatch.setattr(settings, "api_token", TOKEN)
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/ext/"):
            continue
        concrete = (path.replace("{mission_id}", "run_20260101_000000")
                    .replace("{dataset_id:path}", "user/ds").replace("{episode}", "0").replace("{name}", "hello"))
        for method in route.methods - {"HEAD", "OPTIONS"}:
            r = client.request(method, concrete)  # 토큰 없이
            assert r.status_code == 401, f"{method} {path} 가 인증 없이 {r.status_code}"


# ── 미션 = 플러그인 ──


def test_start_mission_starts_the_plugin_with_the_body_as_config(client, authed):
    _install_mission(authed)
    r = client.post("/api/ext/v1/missions", headers=AUTH,
                    json={"type": "recycling", "max_episodes": 5, "dry_run": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"].startswith("run_") and body["plugin"] == "recycling" and body["max_episodes"] == 5
    env = registry._owners["recycling"].env
    cfg = json.loads(env["PIPER_PLUGIN_CONFIG"])
    assert cfg["max_episodes"] == 5 and cfg["dry_run"] is True
    assert env["PIPER_PLUGIN_RUN_ID"] == body["id"]
    # 라이브 조회는 플러그인 상태다
    live = client.get(f"/api/ext/v1/missions/{body['id']}", headers=AUTH).json()
    assert live["live"] is True and live["plugin"] == "recycling" and live["state"] == "running"
    # 두 번째 미션은 409
    assert client.post("/api/ext/v1/missions", headers=AUTH, json={"type": "recycling"}).status_code == 409
    # 취소 = 플러그인 정지
    assert client.post(f"/api/ext/v1/missions/{body['id']}/cancel", headers=AUTH).status_code == 200
    assert registry.live_mission() is None


def test_unknown_mission_type_is_400(client, authed):
    r = client.post("/api/ext/v1/missions", headers=AUTH, json={"type": "popcorn"})
    assert r.status_code == 400


def test_non_mission_plugin_is_refused(client, authed):
    _install_mission(authed, "helper", mission=False)
    r = client.post("/api/ext/v1/missions", headers=AUTH, json={"type": "helper"})
    assert r.status_code == 400 and "mission" in r.json()["detail"]


def test_plugin_refusal_is_passed_through(client, authed, monkeypatch):
    """플러그인 시작이 거부되면(예: GPU 배타) 그 상태 코드 그대로."""
    _install_mission(authed)
    from fastapi import HTTPException

    async def refuse(*a, **k):
        raise HTTPException(409, "학습 중")

    monkeypatch.setattr(registry, "start", refuse)
    assert client.post("/api/ext/v1/missions", headers=AUTH, json={"type": "recycling"}).status_code == 409


def test_finished_mission_reads_journal(client, authed):
    jdir = authed / "plugins" / ".runtime" / "recycling" / "runs"
    jdir.mkdir(parents=True)
    lines = [{"episode": 1, "decision": {"kind": "task"}}, {"episode": 2, "result": {"skipped": True}}]
    (jdir / "run_20260816_010101.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + '\n{"잘린')
    r = client.get("/api/ext/v1/missions/run_20260816_010101", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["live"] is False and body["plugin"] == "recycling"
    assert body["events"] == lines  # 잘린 마지막 줄은 버린다
    listing = client.get("/api/ext/v1/missions", headers=AUTH).json()["missions"]
    assert [m["id"] for m in listing] == ["run_20260816_010101"] and listing[0]["plugin"] == "recycling"


def test_bad_mission_id_is_rejected(client, authed):
    r = client.get("/api/ext/v1/missions/..%2F..%2Fetc", headers=AUTH)
    assert r.status_code in (400, 404)
    assert client.get("/api/ext/v1/missions/run_x", headers=AUTH).status_code == 400


def test_missing_mission_is_404(client, authed):
    assert client.get("/api/ext/v1/missions/run_20990101_000000", headers=AUTH).status_code == 404


def test_cancel_only_hits_live_mission(client, authed):
    assert client.post("/api/ext/v1/missions/run_20990101_000000/cancel", headers=AUTH).status_code == 409


# ── 상태·안전 ──


def test_status_shape(client, authed):
    r = client.get("/api/ext/v1/status", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    for key in ("activities", "estop", "plugins", "mission", "vision"):
        assert key in body
    assert body["orchestrator"] is None      # 한 릴리스 동안 null 로 남긴다 (feature/plugins.md §12-2)


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


# ── 플러그인 직접 제어 (6단계) ──


def test_external_can_start_and_stop_a_plain_plugin_with_saved_config(client, authed):
    _install_mission(authed, "helper", mission=False)
    registry.save_config("helper", {"max_episodes": 3}, {})
    r = client.get("/api/ext/v1/plugins", headers=AUTH)
    assert r.status_code == 200 and any(p["name"] == "helper" and p["mission"] is False for p in r.json()["plugins"])
    r = client.post("/api/ext/v1/plugins/helper/start", headers=AUTH, json={"config": {"extra": 1}})
    assert r.status_code == 200 and r.json()["state"] == "running"
    cfg = json.loads(registry._owners["helper"].env["PIPER_PLUGIN_CONFIG"])
    assert cfg["max_episodes"] == 3 and cfg["extra"] == 1          # 저장값 위에 본문
    assert client.get("/api/ext/v1/plugins/helper", headers=AUTH).json()["state"] == "running"
    assert client.post("/api/ext/v1/plugins/helper/stop", headers=AUTH).json()["state"] == "idle"
    assert client.post("/api/ext/v1/plugins/nope/start", headers=AUTH, json={}).status_code == 404


# ── 읽기 전용 (feature/agent-cli.md §4-4) — MCP 리소스의 재료. GET 만, 장치 수준 없음 ──


def test_dataset_list_is_slim(client, authed, monkeypatch):
    from app.services import dataset_scanner as DS

    monkeypatch.setattr(DS, "scan_datasets", lambda: [{
        "id": "user/apple", "source": "lerobot", "total_episodes": 12, "total_frames": 3400, "fps": 15,
        "features": {"observation.images.top": {}, "observation.images.hand": {}, "observation.state": {}},
        "baked": None, "delta": None, "notes": {"name": "", "description": "", "updated_at": ""}, "scene": None,
        "created": "2026-10-01T00:00:00", "modified": "2026-10-02T00:00:00", "size_bytes": 10, "path": "/srv/x"}])
    r = client.get("/api/ext/v1/datasets", headers=AUTH)
    assert r.status_code == 200
    d = r.json()["datasets"][0]
    assert d["id"] == "user/apple" and d["cameras"] == ["hand", "top"] and d["total_episodes"] == 12
    assert "features" not in d and "path" not in d          # 크고, 외부가 알 일이 아니다


def test_episode_summary_joins_meta_phases_and_notes(client, authed, monkeypatch, tmp_path):
    from piper_phase import labeler as PL

    from app.services import dataset_scanner as DS

    ds = tmp_path / "ds"
    (ds / "meta").mkdir(parents=True)
    monkeypatch.setattr(DS, "episode_meta", lambda dataset_id, episode:
                        (ds, {"fps": 15}, {"episode_index": 3, "length": 210, "tasks": ["사과를 집어라"]}) if episode == 3 else None)
    monkeypatch.setattr(PL, "load", lambda p: {"version": 3, "episodes": {
        "3": {"segments": [[0, 40, 0], [40, 120, 1]], "reviewed": True, "note": "", "cycles": 1}}})
    monkeypatch.setattr(DS, "baked_info", lambda p: None)
    monkeypatch.setattr(DS, "delta_info", lambda p: None)
    r = client.get("/api/ext/v1/datasets/user/apple/episodes/3", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dataset"] == "user/apple" and body["length"] == 210 and body["tasks"] == ["사과를 집어라"]
    assert body["phases"]["reviewed"] is True and body["phases"]["segments"][1]["start"] == 40
    assert isinstance(body["phases"]["segments"][1]["phase"], str)    # 번호가 아니라 이름
    assert body["notes"]["name"] == ""                                 # 사이드카 없음 = 빈 값, 오류 아님
    assert client.get("/api/ext/v1/datasets/user/apple/episodes/9", headers=AUTH).status_code == 404
    monkeypatch.setattr(PL, "load", lambda p: None)
    assert client.get("/api/ext/v1/datasets/user/apple/episodes/3", headers=AUTH).json()["phases"] is None


def test_eval_training_and_context_are_the_internal_payloads(client, authed, monkeypatch, tmp_path):
    from app.routers import eval_log
    from app.services import agent_context as ac
    from app.services.training import train_manager
    from app.services.training.jobs import job_registry

    monkeypatch.setattr(eval_log, "EVAL_FILE", tmp_path / "none.jsonl")
    assert client.get("/api/ext/v1/eval/stats", headers=AUTH).json()["total"] == 0
    monkeypatch.setattr(train_manager, "get_status", lambda: {"state": "idle"})
    monkeypatch.setattr(job_registry, "list", lambda: [])
    assert client.get("/api/ext/v1/training", headers=AUTH).json() == {"status": {"state": "idle"}, "jobs": []}
    # 꾸러미 — /api/system/agent-context 와 같은 본문·같은 400
    monkeypatch.setattr(ac, "collect", lambda **kw: {
        "generated_at": 0.0, "since": kw.get("since") or "30 minutes ago", "unit": kw.get("unit"), "lines": kw.get("lines"),
        "sections": {"pointers": {"docs": ["CLAUDE.md"], "more_logs": "/x", "services": "/s", "version": "/v",
                                  "activity": "/a", "swagger": "/docs"}},
        "errors": {"head": "stub"}})
    r = client.get("/api/ext/v1/diag/context", headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert r.text.startswith("# PIPER 에이전트용 컨텍스트") and "수집 실패: stub" in r.text and "CLAUDE.md" in r.text
    assert client.get("/api/ext/v1/diag/context?format=json", headers=AUTH).json()["unit"] == "all"
    assert client.get("/api/ext/v1/diag/context?unit=sshd", headers=AUTH).status_code == 400
    assert client.get("/api/ext/v1/diag/context?format=xml", headers=AUTH).status_code == 400


# ── 설정 → 에이전트 탭의 재료 (feature/agent-cli.md §4-7) ──


def test_agent_link_tells_whether_ext_is_on_and_the_last_call_but_never_the_token(client, authed, monkeypatch):
    r = client.get("/api/system/agent-link")
    assert r.status_code == 200
    body = r.json()
    assert body["ext_enabled"] is True and TOKEN not in r.text
    assert "piper_mission_start" in body["mcp"]["tools"] and "piper://context" in body["mcp"]["resources"]
    client.get("/api/ext/v1/status", headers=AUTH)
    last = client.get("/api/system/agent-link").json()["last_ext_call"]
    assert last["method"] == "GET" and last["path"] == "/api/ext/v1/status" and last["at"] > 0
    # 틀린 토큰은 "마지막 호출" 로 치지 않는다
    client.get("/api/ext/v1/missions", headers={"Authorization": "Bearer wrong"})
    assert client.get("/api/system/agent-link").json()["last_ext_call"]["path"] == "/api/ext/v1/status"
    monkeypatch.setattr(settings, "api_token", "")
    assert client.get("/api/system/agent-link").json()["ext_enabled"] is False
