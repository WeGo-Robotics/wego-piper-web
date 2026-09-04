"""정렬 검사 흐름 — 만들기·기준·검사 (feature/alignment-check.md).

⚠ **이 기능은 팔을 실제로 움직인다.** 그래서 여기서 지키는 것은 계산이 아니라
**언제 안 움직이는가** 다 — 거절해야 할 때 거절하는지.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from conftest import python_code_only


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    from app.services import presets

    monkeypatch.setattr(presets, "PRESETS_ROOT", tmp_path / "presets")


def _check(**kw) -> dict:
    return {"name": "front", "iface": "can0", "camera_id": "cam0",
            "tag_id": 3, "tag_mm": 40.0, "family": "36h11",
            "pose": {"joint1": 0.0}, "baseline": None, "last": None, **kw}


def test_it_refuses_while_the_arm_is_moving(client, monkeypatch):
    """⚠ 추론·녹화·텔레옵 중에 팔을 자세로 보내면 그 작업이 망가진다 —
    `/zero` 와 같은 판단이다."""
    from app.services import exclusivity, presets
    from app.services.alignment import DOMAIN

    presets.save(DOMAIN, "front", _check(baseline={"at": 1.0, "pose": {
        "tag_id": 3, "x_mm": 0, "y_mm": 0, "z_mm": 300, "rvec": [0, 0, 0]}}))
    monkeypatch.setattr(exclusivity, "running",
                        lambda: [exclusivity.Activity.RECORDING])

    r = client.post("/api/alignment/front/run")
    assert r.status_code == 409, r.text
    assert "녹화" in r.json()["detail"]


def test_a_check_without_a_baseline_will_not_move_the_arm(client, monkeypatch):
    """⚠ 기준이 없으면 잴 대상이 없다. 그런데도 팔을 보내면 **아무 소득 없이
    팔만 움직이는** 셈이다 — 물리적으로 움직이는 동작은 이유가 있어야 한다."""
    from app.services import presets
    from app.services.alignment import DOMAIN
    import app.services.alignment as al

    presets.save(DOMAIN, "front", _check())
    moved = []
    monkeypatch.setattr(al, "move_to", lambda *a: moved.append(a))

    r = client.post("/api/alignment/front/run")
    assert r.status_code == 409, r.text
    assert "기준" in r.json()["detail"]
    assert moved == [], "기준도 없는데 팔을 움직였다"


def test_it_refuses_a_camera_whose_optics_are_unknown(client, monkeypatch):
    """⚠ 초점거리를 지어내면 mm 단위 답이 **그럴듯한 모양으로** 틀린다 —
    틀린 줄도 모르게 된다. 모르면 거절한다."""
    import app.services.alignment as al
    from app.services import presets
    from app.services.alignment import DOMAIN

    presets.save(DOMAIN, "front", _check(baseline={"at": 1.0, "pose": {
        "tag_id": 3, "x_mm": 0, "y_mm": 0, "z_mm": 300, "rvec": [0, 0, 0]}}))
    monkeypatch.setattr(al, "intrinsics_for", lambda cid: None)
    moved = []
    monkeypatch.setattr(al, "move_to", lambda *a: moved.append(a))

    r = client.post("/api/alignment/front/run")
    assert r.status_code == 409 and "내부 파라미터" in r.json()["detail"]
    assert moved == [], "잴 수도 없는데 팔을 움직였다"


def test_the_pose_is_captured_not_typed(client):
    """⚠ 관절값을 손으로 적게 하면 오타 하나가 팔을 엉뚱한 곳으로 보낸다.
    자세는 **지금 자세를 찍어서** 만든다 — API 가 자세를 받지 않는다."""
    import inspect

    from app.routers.alignment import CheckBody, create_check

    assert "pose" not in CheckBody.model_fields, "API 가 자세를 받는다"
    # 자세를 읽는 자리는 `_read_pose` 로 옮겼다 (저장된 자세와 나눠 쓰려고).
    # 받는 것은 **이름**뿐이고 관절값이 아니다.
    from app.routers.alignment import _read_pose

    assert "read_joints_normalized" in inspect.getsource(_read_pose)
    assert "_read_pose" in inspect.getsource(create_check)


def test_the_gripper_is_not_part_of_the_pose():
    """그리퍼가 열려 있든 닫혀 있든 엔드이펙터 위치는 같아야 한다 — 그걸
    자세에 넣으면 무관한 차이로 검사가 실패한다."""
    import inspect

    from app.routers.alignment import _read_pose

    assert 'k != "gripper"' in inspect.getsource(_read_pose)


def test_moving_goes_through_the_safety_filter():
    """⚠ `arm.JointCtrl` 로 직접 보내면 바닥·범위·변화율·데드맨이 전부 빠진다.
    `JogSession` 을 거쳐야 robotd 의 `filter_goal` 을 탄다."""
    import inspect

    from app.services.alignment import move_to

    # 주석·docstring 을 걷어낸다 — 이 함수는 docstring 에서 `JointCtrl` 을
    # **쓰지 말라고** 적고 있어서, 그대로 뒤지면 그 설명이 검사에 걸린다.
    src = python_code_only(inspect.getsource(move_to))
    assert "jog_session" in src, "안전 필터를 우회한다"
    assert "JointCtrl" not in src


def test_it_will_not_measure_from_the_wrong_pose():
    """⚠ 목표에 못 갔는데 재면, 그 자세 차이가 그대로 "틀어짐" 으로 기록된다.
    오진을 만드느니 측정을 포기하는 쪽이 낫다."""
    import inspect

    from app.services.alignment import move_to

    src = inspect.getsource(move_to)
    assert "도달하지 못했습니다" in src and "측정하지 않습니다" in src
