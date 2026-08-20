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
    # model 은 yolod 자기소개 (없으면 None) — 데모 화면이 모델 세부 정보를 그린다
    assert {"state", "pid", "cams", "model"} <= r.keys()


def test_models_catalog_shape(client):
    """시작 UI 선택지 — 표준 카탈로그(현행 + 과거 세대) + 로컬 존재 여부."""
    models = client.get("/api/vision/models").json()["models"]
    files = [m["file"] for m in models]
    # 세대별 대표가 있고, 로컬에 없는 모델도 목록에 나온다 (자동 다운로드되므로)
    assert {"yolo11n.pt", "yolo11x.pt", "yolov8n.pt", "yolov5nu.pt"} <= set(files)
    assert len(files) == len(set(files))
    for m in models:
        assert {"family", "file", "label", "params_m", "size_mb", "downloaded"} <= m.keys()
        assert isinstance(m["downloaded"], bool)


def test_segments_is_a_list(client):
    assert isinstance(client.get("/api/vision/segments").json()["segments"], list)


def test_unknown_preview_is_404(client):
    assert client.get("/api/vision/preview/no-such-cam").status_code == 404


def test_unknown_segment_snapshot_is_404(client):
    assert client.get("/api/vision/segments/no-such-seg/snapshot").status_code == 404


def test_custom_model_upload_list_resolve_delete(client, monkeypatch, tmp_path):
    """커스텀 가중치 왕복: 업로드 → 커스텀으로 목록 → 시작 시 절대경로 해석 → 삭제."""
    from app.core.config import settings as cfg
    from app.routers import vision

    monkeypatch.setattr(cfg, "yolo_models_dir", tmp_path)

    r = client.put("/api/vision/models/best.pt", content=b"weights-bytes")
    assert r.status_code == 200
    assert r.json()["file"] == "best.pt"
    assert (tmp_path / "best.pt").read_bytes() == b"weights-bytes"

    custom = [m for m in client.get("/api/vision/models").json()["models"]
              if m["family"] == "커스텀"]
    assert [m["file"] for m in custom] == ["best.pt"]
    assert custom[0]["downloaded"] is True

    # 커스텀은 디렉토리 안 절대경로로, 표준·미지 이름은 그대로 (ultralytics 몫)
    assert vision._resolve_model("best.pt") == str(tmp_path / "best.pt")
    assert vision._resolve_model("yolo11n.pt") == "yolo11n.pt"

    assert client.delete("/api/vision/models/best.pt").status_code == 200
    assert not (tmp_path / "best.pt").exists()
    assert client.delete("/api/vision/models/best.pt").status_code == 404


def test_upload_rejects_bad_names(client, monkeypatch, tmp_path):
    from app.core.config import settings as cfg

    monkeypatch.setattr(cfg, "yolo_models_dir", tmp_path)
    # .pt 아님 / 표준 이름 충돌 / 빈 파일
    assert client.put("/api/vision/models/weights.bin", content=b"x").status_code == 400
    assert client.put("/api/vision/models/yolo11n.pt", content=b"x").status_code == 400
    assert client.put("/api/vision/models/empty.pt", content=b"").status_code == 400
    assert list(tmp_path.iterdir()) == []  # 실패한 업로드가 흔적을 남기면 안 된다


def test_resolve_model_blocks_path_characters(monkeypatch, tmp_path):
    """클라이언트가 보낸 이름이 경로로 새면 임의 파일 로드가 된다."""
    from fastapi import HTTPException

    from app.core.config import settings as cfg
    from app.routers import vision

    monkeypatch.setattr(cfg, "yolo_models_dir", tmp_path)
    for bad in ["../etc.pt", "a/b.pt", ".hidden.pt", "..\\win.pt"]:
        with pytest.raises(HTTPException):
            vision._resolve_model(bad)
