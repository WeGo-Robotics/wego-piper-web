"""작업별 카메라 프로파일 — 수집·추론 시작 전에 1회 적용 (feature/camera-profiles.md 후속).

노출·WB 가 학습 데이터와 다르면 정책이 다른 분포를 본다. 그래서 작업이
프로파일을 지정하면 `prepare_cameras` **뒤에** 한 번 밀어 넣고 시작하고,
없는 프로파일이면 시작하지 않는다 — 기준 없이 찍힌 에피소드가 조용히
섞이는 것보다 낫다.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _stub_camera_prep(monkeypatch):
    from app.services import camera_config
    monkeypatch.setattr(camera_config, "check_camera_config", lambda *a, **k: None)
    monkeypatch.setattr(camera_config, "prepare_cameras", lambda *a, **k: None)


def test_both_start_requests_carry_the_profile_and_default_to_off():
    """빈 값 = 적용 안 함. 낡은 프론트가 필드를 안 보내도 그대로 동작해야 한다."""
    from app.routers.models import InferenceStartRequest
    from app.routers.recording import RecordStartRequest

    assert RecordStartRequest(repo_id="a/b").camera_profile == ""
    assert InferenceStartRequest(checkpoint_path="x").camera_profile == ""


def test_a_missing_profile_returns_an_error_not_a_crash():
    """지정한 이름이 없으면 `error` 로 말한다 — 호출부(시작 라우트)가 이걸로 막는다."""
    from app.services import camera_profiles

    out = camera_profiles.apply_for_task("이런-프로파일-없음")
    assert out.get("error"), "없는 프로파일이 조용히 통과했다"


def test_recording_refuses_to_start_without_the_named_profile(client, monkeypatch):
    """⚠ 프로파일이 없는데 시작하면 **기준 없이 찍힌 에피소드**가 데이터셋에
    섞인다 — 삭제한 프로파일 이름이 프론트 저장값에 남은 경우가 정확히 이 경로다."""
    _stub_camera_prep(monkeypatch)
    r = client.post("/api/recording/start", json={
        "repo_id": "user/ds", "single_task": "테스트",
        "robot_port": "can0", "teleop_port": "can1",
        "camera_profile": "지워진-프로파일",
    })
    assert r.status_code == 400
    assert "지워진-프로파일" in r.json()["detail"]


def test_recording_applies_the_profile_before_touching_the_arms(client, monkeypatch):
    """적용은 카메라 연결 **뒤**, 팔 준비 **앞**이다 — 팔 준비에서 멈춰도
    프로파일은 이미 장치에 들어가 있어야 순서가 증명된다."""
    _stub_camera_prep(monkeypatch)
    called = {}

    from app.services import camera_profiles, robot_config

    def _fake_apply(name):
        called["profile"] = name
        return {"profile": name, "cameras": []}
    monkeypatch.setattr(camera_profiles, "apply_for_task", _fake_apply)

    def _boom(*a, **k):
        raise robot_config.ArmPrepareError("팔 준비 중단(테스트)")
    monkeypatch.setattr(robot_config, "prepare_arms", _boom)

    r = client.post("/api/recording/start", json={
        "repo_id": "user/ds", "single_task": "테스트",
        "robot_port": "can0", "teleop_port": "can1",
        "camera_profile": "조명-기본",
    })
    assert r.status_code == 400 and "팔 준비 중단" in r.json()["detail"]
    assert called.get("profile") == "조명-기본", "프로파일 적용이 팔 준비보다 먼저 불리지 않았다"


def test_inference_start_takes_the_same_path():
    """추론 시작도 같은 규칙을 탄다 — 한쪽만 고쳐지는 것을 소스에서 막는다."""
    root = Path(__file__).resolve().parents[1] / "app" / "routers"
    for f in ("recording.py", "models.py"):
        src = (root / f).read_text()
        assert "apply_for_task" in src, f"{f} 가 작업 프로파일을 적용하지 않는다"
        assert "camera_profile" in src
