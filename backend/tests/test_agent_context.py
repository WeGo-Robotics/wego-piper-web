"""에이전트용 컨텍스트 꾸러미 (feature/agent-cli.md §4-1) — 모으기만 한다, 비밀은 없다, 하나가 죽어도 나머지는 그린다."""

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import agent_context as ac

TOKEN = "ext-token-SECRET-0123456789"
KEY = "sk-ant-api03-SECRETKEY-abcdefghijklmnop"


@pytest.fixture
def quiet(monkeypatch):
    """기계를 읽는 수집기를 전부 가짜로 — 테스트는 이 기계의 유닛·버스·저널을 보지 않는다."""
    from app.services import exclusivity, inference_state, syslog, units, version
    from app.services.estop_bridge import estop_bridge
    from app.services.plugins import registry

    monkeypatch.setattr(version, "collect", lambda: {
        "gateway": {"version": "v0.5.21", "source": "git"},
        "staleness": {"ok": True, "wheels": [], "daemons": None}, "daemons": {"robotd": {}}})
    monkeypatch.setattr(units, "list_units", lambda: [
        units.Unit(name="robotd", active=True, since=1000.0, stale=True, code_mtime=2000.0, pid=42, description="PIPER 팔 (CAN)")])
    monkeypatch.setattr(units, "gateway_status", lambda: {
        "pid": 1, "since": 1000.0, "code_mtime": None, "stale": False, "age_s": 5, "restartable": False})
    monkeypatch.setattr(exclusivity, "snapshot", lambda: {"running": ["inference"], "blocked": {}, "labels": {"inference": "추론"}})
    monkeypatch.setattr(inference_state, "get", lambda: inference_state.RunningPolicy("act", "/data/models/act/checkpoints/last"))
    monkeypatch.setattr(estop_bridge, "status", lambda: {
        "bus_available": True, "armed": False, "last_trigger": {"reason": "heartbeat_timeout", "at": 123.0},
        "timeout_ms": 2500, "heartbeat_interval_ms": 500})
    monkeypatch.setattr(registry, "live_mission", lambda: None)
    monkeypatch.setattr(registry, "running", lambda: {})
    monkeypatch.setattr(registry, "runs", lambda name=None: [])
    monkeypatch.setattr(syslog, "fetch", lambda unit, lines, level, since: {
        "entries": [
            {"t": 1700000000.0, "unit": "robotd", "level": "error", "msg": f"Authorization: Bearer {TOKEN} 로 실패", "count": 3},
            {"t": 1700000001.0, "unit": "gateway", "level": "warning", "msg": "카메라 top 세그먼트가 없습니다"},
        ],
        "scanned": 2, "truncated": False, "source": "journal", "via": "local"})
    monkeypatch.setattr(ac, "_devices", lambda: {
        "summary": {"robots": {"ok": 1, "warn": 0}, "cameras": {"ok": 1, "warn": 1}, "alerts": 1},
        "alerts": [{"kind": "camera", "id": "rs:1:color", "name": "top", "reason": "device_gone", "text": "카메라 top 이 사라졌습니다"}],
        "arms": [{"iface": "can0", "role": "follower", "slot": None, "side": None, "connected": True, "state": "UP", "transport": "can"}],
        "cameras": [{"id": "rs:1:color", "name": "D405 Color", "stream": "top", "type": "realsense", "present": False, "connected": False}]})
    monkeypatch.setattr(settings, "api_token", TOKEN)
    monkeypatch.setattr(settings, "anthropic_api_key", KEY)


def test_markdown_has_the_sections_in_order_and_hides_secrets(quiet):
    data = ac.collect(since="10 minutes ago", unit="all", lines=50)
    md = ac.render_markdown(data)
    titles = [h.split(". ", 1)[1] for h in md.splitlines() if h.startswith("## ")]
    assert titles == ["머리", "유닛", "활동", "E-stop", "장치", "저널", "미션 저널", "읽을 곳"]
    assert "v0.5.21" in md and "robotd" in md and "추론" in md and "/data/models/act" in md
    assert "heartbeat_timeout" in md and "카메라 top 이 사라졌습니다" in md and "×3" in md
    # 비밀은 설정값이든 토큰 모양이든 지워진다
    assert TOKEN not in md and KEY not in md and "Bearer ***" in md
    # 추론이 돌고 있으면 그 문서를 가리킨다
    assert "feature/camera-resolution.md" in md and "CLAUDE.md" in md


def test_json_is_redacted_too(quiet):
    data = ac.redact_data(ac.collect())
    assert TOKEN not in json.dumps(data, ensure_ascii=False)
    assert data["sections"]["journal"]["entries"][0]["msg"].startswith("Authorization: Bearer ***")


def test_one_failing_collector_does_not_sink_the_bundle(quiet, monkeypatch):
    from app.services import version

    def boom():
        raise RuntimeError("unitd 불통")

    monkeypatch.setattr(version, "collect", boom)
    data = ac.collect()
    assert data["errors"]["head"] == "unitd 불통" and "units" in data["sections"]
    md = ac.render_markdown(data)
    assert "수집 실패" in md and "unitd 불통" in md and "## 2. 유닛" in md


def test_inbox_is_section_zero_and_lines_are_capped(quiet):
    inbox = [{"at": 1700000000.0, "level": "error", "text": f"토큰 {TOKEN} 유출", "source": "추론"}]
    data = ac.collect(lines=99999, inbox=inbox)
    assert data["lines"] == ac.MAX_LINES
    md = ac.render_markdown(data)
    assert md.index("## 0. 브라우저 알림함") < md.index("## 1. 머리")
    assert TOKEN not in md and "토큰 *** 유출" in md


def test_route_serves_markdown_and_json_and_rejects_unknown_unit(quiet):
    client = TestClient(app)
    r = client.get("/api/system/agent-context?since=1 hour ago")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown") and r.text.startswith("# PIPER 에이전트용 컨텍스트")
    r = client.post("/api/system/agent-context", json={"format": "json", "inbox": [{"level": "warn", "text": "x"}]})
    assert r.status_code == 200 and r.json()["sections"]["inbox"][0]["text"] == "x"
    assert client.get("/api/system/agent-context?unit=sshd").status_code == 400
    assert client.get("/api/system/agent-context?format=xml").status_code == 400
