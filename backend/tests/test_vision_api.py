"""비전·판단 테스트 API — 라우터 계약 (프로세스는 띄우지 않는다)."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import llm_client
from app.services.llm_client import LLMJudgeError


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def test_judge_defaults_expose_rules_and_settings(client):
    r = client.get("/api/vision/judge/defaults").json()
    assert "분리수거" in r["rules"]
    assert r["provider"] and r["model"]


def test_judge_returns_slots_and_latency(client, monkeypatch):
    from app.routers.vision import JudgeSlots

    captured = {}

    async def fake_judge(system, user, schema, *, timeout_s, provider, model):
        captured.update(system=system, user=user, provider=provider, model=model)
        return JudgeSlots(target="bottle", destination="plastic_bin", reason="테스트")

    monkeypatch.setattr(llm_client, "judge", fake_judge)
    r = client.post("/api/vision/judge", json={
        "user": "[top] bottle(0.9)", "provider": "openai_compat", "model": "qwen2.5:7b",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["slots"]["target"] == "bottle"
    assert body["ms"] >= 0
    assert body["model"] == "qwen2.5:7b"
    assert captured["user"] == "[top] bottle(0.9)"
    assert "분리수거" in captured["system"]  # 기본 규칙이 system 으로


def test_judge_error_carries_reason(client, monkeypatch):
    async def fake_judge(*a, **k):
        raise LLMJudgeError("refusal", "category=cyber")

    monkeypatch.setattr(llm_client, "judge", fake_judge)
    r = client.post("/api/vision/judge", json={"user": "x"})
    assert r.status_code == 502
    assert "refusal" in r.json()["detail"]


def test_start_requires_cameras(client):
    r = client.post("/api/vision/start", json={"cams": {}})
    assert r.status_code == 400


def test_status_shape(client):
    r = client.get("/api/vision/status").json()
    assert {"state", "pid", "cams"} <= r.keys()


def test_segments_is_a_list(client):
    assert isinstance(client.get("/api/vision/segments").json()["segments"], list)


def test_unknown_preview_is_404(client):
    assert client.get("/api/vision/preview/no-such-cam").status_code == 404
