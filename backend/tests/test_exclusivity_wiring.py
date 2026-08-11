"""라우터가 실제로 배타 규칙을 부르는지 — 배선 테스트.

test_exclusivity.py 는 표 자체를 본다. 표가 맞아도 라우터가 `require_idle()` 을
안 부르면 아무 소용이 없다. 여기서는 프로세스 상태를 가짜로 켜고 HTTP 로 두드린다.

프로세스를 실제로 띄우지 않는다 — 전부 409 로 막히는 경로만 확인한다.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.process_manager import ProcessState


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def pretend_running():
    """지정한 매니저를 RUNNING 으로 만들고 테스트 후 되돌린다."""
    from app.services import dataset_jobs
    from app.services.policy_server_manager import policy_server_manager
    from app.services.process_manager import process_manager
    from app.services.record_manager import record_manager
    from app.services.training import train_manager

    pms = {
        "inference": process_manager,
        "recording": record_manager.pm,
        "training": train_manager.runner.pm,
        "policy_server": policy_server_manager.pm,
        "dataset_edit": dataset_jobs.edit_pm,
        "upload": dataset_jobs.upload_pm,
    }
    original = {k: pm.state for k, pm in pms.items()}

    def _set(name: str):
        pms[name]._state = ProcessState.RUNNING

    yield _set

    for k, pm in pms.items():
        pm._state = original[k]


def test_activity_endpoint_reflects_state(client, pretend_running):
    assert client.get("/api/activity").json()["running"] == []

    pretend_running("recording")
    snap = client.get("/api/activity").json()
    assert snap["running"] == ["recording"]
    assert snap["blocked"]["training"] == ["recording"]
    assert snap["blocked"]["inference"] == ["recording"]
    assert snap["blocked"]["upload"] == []


def test_recording_blocks_training_endpoints(client, pretend_running):
    pretend_running("recording")
    for path, body in [
        ("/api/training/start", {"dataset_repo_id": "org/ds"}),
        ("/api/training/start-custom", {"args": ["--x"]}),
    ]:
        r = client.post(path, json=body)
        assert r.status_code == 409, path
        assert "녹화" in r.json()["detail"]


def test_recording_blocks_inference_start_custom(client, pretend_running):
    """가장 중요한 회귀 — 이 엔드포인트에는 가드가 아예 없었고,
    무조건 `_release_all_cameras()` 를 불러 녹화 중에도 카메라를 뺏었다."""
    pretend_running("recording")
    r = client.post("/api/models/inference/start-custom", json={"args": ["--x"]})
    assert r.status_code == 409
    assert "녹화" in r.json()["detail"]


def test_recording_blocks_camera_device_access(client, pretend_running):
    """D405 가 D-state 로 물리는 것을 막는 기존 가드가 유지돼야 한다."""
    pretend_running("recording")
    r = client.post("/api/cameras/connect", json={"id": "/dev/video0"})
    assert r.status_code == 409
    assert "카메라" in r.json()["detail"]


def test_training_blocks_policy_server(client, pretend_running):
    """정책 서버는 이전에 어느 가드에도 없었다 — 학습과 GPU 를 동시에 잡을 수 있었다."""
    pretend_running("training")
    r = client.post("/api/policy-server/start", json={})
    assert r.status_code == 409
    assert "학습" in r.json()["detail"]


def test_inference_blocks_dataset_edit(client, pretend_running):
    """편집이 추론과 같은 전역 ProcessManager 를 써서 핸들을 덮어썼다."""
    pretend_running("inference")
    r = client.post(
        "/api/datasets/org/ds/edit", json={"operation": "info", "params": {}}
    )
    assert r.status_code == 409
    assert "추론" in r.json()["detail"]


def test_upload_does_not_block_training(client, pretend_running):
    """업로드는 네트워크/디스크만 쓴다 — 학습을 막으면 안 된다."""
    pretend_running("upload")
    assert client.get("/api/activity").json()["blocked"]["training"] == []


def test_error_message_uses_correct_korean_particle(client, pretend_running):
    """"정책 서버을(를)" 같은 문구가 나오지 않아야 한다."""
    pretend_running("training")
    detail = client.post("/api/policy-server/start", json={}).json()["detail"]
    assert "(를)" not in detail and "(가)" not in detail
    assert "정책 서버를 시작하세요" in detail
