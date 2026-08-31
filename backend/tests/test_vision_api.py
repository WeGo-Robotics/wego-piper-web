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
    # 로컬에 없는 모델도 목록에 나온다 (고르면 받는다)
    assert {"PekingU/rtdetr_v2_r18vd", "PekingU/rtdetr_v2_r101vd"} <= set(files)
    assert not [f for f in files if f.endswith(".pt")], "ultralytics 가중치가 남아 있다"
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


def test_custom_model_list_resolve_delete(client, monkeypatch, tmp_path):
    """커스텀 가중치 왕복: 목록 → 시작 시 절대경로 해석 → 삭제.

    ⚠ **업로드는 없어졌다.** `.pt` 원시 바이트를 받던 엔드포인트였는데, 그 형식은
    ultralytics 것이라 더 이상 열 수 없다. 커스텀 가중치는 학습이 만든다.
    """
    import json as _json

    from app.core.config import settings as cfg
    from app.routers import vision

    monkeypatch.setattr(cfg, "yolo_models_dir", tmp_path)
    d = tmp_path / "best-0831"
    d.mkdir()
    (d / "model.safetensors").write_bytes(b"weights-bytes")
    (d / "piper_meta.json").write_text(_json.dumps({"dataset": "x", "classes": ["a"]}))

    custom = [m for m in client.get("/api/vision/models").json()["models"]
              if m["family"] == "커스텀"]
    assert [m["file"] for m in custom] == ["best-0831"]
    assert custom[0]["downloaded"] is True

    assert vision._resolve_model("best-0831") == str(d)
    assert vision._resolve_model("PekingU/rtdetr_v2_r18vd") == "PekingU/rtdetr_v2_r18vd"

    assert client.delete("/api/vision/models/best-0831").status_code == 200
    assert not d.exists()
    assert client.delete("/api/vision/models/best-0831").status_code == 404


def test_a_half_finished_run_is_not_offered(client, monkeypatch, tmp_path):
    """⚠ 학습이 중간에 죽으면 **최고 에폭까지 저장된 디렉토리**가 남는다. 그것도
    모델처럼 보여서 목록에 뜨면, 고르고 나서야 이상하다는 걸 안다. 메타는 완료
    시점에만 쓰이므로 그 존재가 "끝까지 돈 학습"의 표시다."""
    from app.core.config import settings as cfg
    from app.routers import vision

    monkeypatch.setattr(cfg, "yolo_models_dir", tmp_path)
    half = tmp_path / "killed-0831"
    half.mkdir()
    (half / "model.safetensors").write_bytes(b"w")      # piper_meta.json 없음

    custom = [m for m in client.get("/api/vision/models").json()["models"]
              if m["family"] == "커스텀"]
    assert custom == [], "반쪽 학습 결과가 목록에 떴다"
    with pytest.raises(Exception):
        vision._resolve_model("killed-0831")


def test_custom_model_meta_enriches_catalog(client, monkeypatch, tmp_path):
    """학습 유닛이 남긴 메타(mAP·클래스·데이터셋)가 드롭다운 설명이 된다.

    ⚠ 메타는 가중치 디렉토리 **안**에 둔다. 밖에 두면 가중치를 지울 때 고아로
    남아 다음 가중치를 오염시킨다.
    """
    import json as _json

    from app.core.config import settings as cfg

    monkeypatch.setattr(cfg, "yolo_models_dir", tmp_path)
    d = tmp_path / "recycle-0820"
    d.mkdir()
    (d / "piper_meta.json").write_text(_json.dumps({
        "dataset": "recycle", "map50": 0.82, "classes": ["pet", "can"],
    }))

    (custom,) = [m for m in client.get("/api/vision/models").json()["models"]
                 if m["family"] == "커스텀"]
    assert (custom["map50"], custom["classes_n"], custom["trained_on"]) == (0.82, 2, "recycle")

    client.delete("/api/vision/models/recycle-0820")
    assert not d.exists()


def test_resolve_refuses_anything_not_offered(client, monkeypatch, tmp_path):
    """⚠ **화이트리스트다.** 예전에는 "`/` 가 들어 있으면 거절" 이었는데, HF 모델
    id 는 `/` 를 포함한다 — 막는 쪽을 적으면 새 우회를 뒤늦게 알아채게 된다.
    카탈로그에 있거나 커스텀에 있는 것만 통과한다.
    """
    from app.core.config import settings as cfg
    from app.routers import vision

    monkeypatch.setattr(cfg, "yolo_models_dir", tmp_path)
    for bad in ("../../etc/passwd", "/etc/passwd", "yolo11n.pt", "aeiou"):
        with pytest.raises(Exception):
            vision._resolve_model(bad)
