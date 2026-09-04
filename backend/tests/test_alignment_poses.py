"""정렬 자세 프리셋 — 조그로 만들어 재사용한다 (feature/alignment-check.md).

⚠ **자세는 손으로 적는 것이 아니다.** 숫자를 적게 하면 오타 하나가 팔을 엉뚱한
곳으로 보낸다. 팔을 실제로 움직여 눈으로 보고 저장한다 — 그래서 여기서 지키는
것은 "저장된 값이 정말 그 팔의 그 자세인가" 다.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from conftest import code_only


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    from app.services import presets

    monkeypatch.setattr(presets, "PRESETS_ROOT", tmp_path / "presets")


class _Arm:
    connected = True

    def __init__(self, pose):
        self._pose = pose

    def read_joints_normalized(self):
        return dict(self._pose)


def _arms(monkeypatch, **arms):
    from app.services.robot_manager import robot_manager

    monkeypatch.setattr(robot_manager, "arms",
                        {k: _Arm(v) for k, v in arms.items()}, raising=False)


POSE = {"joint1": 1.0, "joint2": -2.0, "joint3": 3.0,
        "joint4": 0.0, "joint5": 5.0, "joint6": 6.0, "gripper": 50.0}


def test_a_saved_pose_holds_the_arms_actual_joints(client, monkeypatch):
    _arms(monkeypatch, can0=POSE)
    r = client.post("/api/alignment/poses", json={"name": "front", "iface": "can0"})
    assert r.status_code == 200, r.text

    got = client.get("/api/alignment/poses").json()["poses"]
    assert len(got) == 1
    saved = got[0]["pose"]
    assert saved["joint5"] == 5.0
    # ⚠ 그리퍼는 뺀다 — 검사는 팔의 자세를 보는 것이고, 그리퍼가 열려 있든
    #   닫혀 있든 엔드이펙터 위치는 같아야 한다.
    assert "gripper" not in saved


def test_poses_are_listed_per_arm(client, monkeypatch):
    """⚠ 팔이 다르면 같은 관절값이 **다른 곳**을 가리킨다 — 캘리브레이션은 같아도
    팔이 놓인 위치가 다르다. 남의 팔 자세를 고르면 팔이 엉뚱한 데로 간다."""
    _arms(monkeypatch, can0=POSE, can3=POSE)
    client.post("/api/alignment/poses", json={"name": "a", "iface": "can0"})
    client.post("/api/alignment/poses", json={"name": "b", "iface": "can3"})

    only0 = client.get("/api/alignment/poses?iface=can0").json()["poses"]
    assert [p["name"] for p in only0] == ["a"]
    assert len(client.get("/api/alignment/poses").json()["poses"]) == 2


def test_a_check_built_on_a_saved_pose_uses_that_pose(client, monkeypatch):
    """⚠ 저장된 자세를 골랐는데 **지금 자세**로 만들어지면, 팔이 그 사이 움직인
    만큼 검사가 엉뚱한 곳을 본다 — 그리고 아무도 그걸 눈치채지 못한다."""
    _arms(monkeypatch, can0=POSE)
    client.post("/api/alignment/poses", json={"name": "front", "iface": "can0"})

    # 저장 뒤 팔이 움직였다
    _arms(monkeypatch, can0={**POSE, "joint5": 99.0})
    r = client.post("/api/alignment", json={
        "name": "chk", "iface": "can0", "camera_id": "cam0",
        "tag_id": 3, "pose_name": "front"})
    assert r.status_code == 200, r.text
    assert r.json()["pose"]["joint5"] == 5.0, "지금 자세로 만들어졌다"


def test_an_unknown_pose_is_refused_not_silently_replaced(client, monkeypatch):
    """⚠ 없는 자세를 골랐을 때 조용히 현재 자세로 대체하면, 사람은 저장된 자세로
    만들어졌다고 믿는다. 거절해야 그 자리에서 안다."""
    _arms(monkeypatch, can0=POSE)
    r = client.post("/api/alignment", json={
        "name": "chk", "iface": "can0", "camera_id": "cam0", "pose_name": "없음"})
    assert r.status_code == 404


def test_deleting_a_pose_does_not_touch_the_checks(client, monkeypatch):
    """검사는 자세를 **복사해** 갖는다 — 자세를 지웠다고 검사가 못 돌면 안 된다."""
    _arms(monkeypatch, can0=POSE)
    client.post("/api/alignment/poses", json={"name": "front", "iface": "can0"})
    client.post("/api/alignment", json={
        "name": "chk", "iface": "can0", "camera_id": "cam0", "pose_name": "front"})
    assert client.delete("/api/alignment/poses/front").status_code == 200

    chk = client.get("/api/alignment").json()["checks"][0]
    assert chk["name"] == "chk"


def test_visible_tags_reports_why_instead_of_crashing(client):
    """⚠ 내부 파라미터가 없거나 프레임을 못 읽는 것은 **흔한 일**이다. 500 을
    던지면 자세 만들기 창이 통째로 죽는다 — 사유를 돌려주고 화면이 말하게 한다."""
    r = client.get("/api/alignment/tags/nosuchcam")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tags"] == []
    assert body.get("error"), "왜 못 봤는지 안 알려준다"


# ── 자세 만들기 창 ───────────────────────────────────────────────────────────

def _modal() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[2]
            / "frontend/src/components/AlignmentPoseModal.tsx").read_text()


def test_the_modal_always_releases_the_jog_session():
    """⚠ **조그 세션은 명령 경로를 점유한다** — 추론·녹화가 막힌다. 창을 닫을 때
    안 놓으면 세션이 영영 남고, 사용자는 왜 추론이 안 되는지 모른다."""
    src = code_only(_modal())
    assert "jog/stop" in src, "조그를 놓는 길이 없다"
    cleanup = src.split("jog/start", 1)[1]
    assert "return () =>" in cleanup and "jog/stop" in cleanup, \
        "언마운트에서 안 놓는다"
    assert ".catch(() => {})" in cleanup.split("jog/stop", 1)[1][:80], \
        "놓다가 실패하면 세션이 남는다 — 삼켜야 한다"


def test_the_modal_shows_what_the_camera_sees():
    """⚠ 태그가 안 보이는 자세로 검사를 만들면 **실행할 때가 되어서야** "태그가
    안 보입니다" 를 만난다 — 그때는 그 자세가 왜 그렇게 정해졌는지도 잊은 뒤다."""
    src = code_only(_modal())
    assert "/stream" in src, "카메라 영상이 없다"
    assert "alignment/tags/" in src, "보이는 태그를 안 보여준다"


def test_the_modal_keeps_the_jog_session_alive():
    """⚠ **한 번 열고 마는 것으로는 부족했다.** 세션을 `useEffect` 에서 여는데
    StrictMode 가 effect 를 두 번 돌린다 — `start → stop → start` 가 되고, 늦게
    도착한 `stop` 이 두 번째 `start` 를 덮으면 세션은 닫혔는데 화면은 열린 줄
    안다. **슬라이더는 움직이는데 팔은 가만히 있는다.** JogPanel 은 버튼 클릭으로
    열어서 이 문제를 안 만난다 — 그래서 조그 창은 되고 새 자세 창은 안 됐다.

    세션은 5 분 놀면 서버가 닫기도 한다. 상태를 보고 없으면 다시 여는 편이
    둘 다 낫다.
    """
    src = code_only(_modal())
    assert "jog/status" in src, "세션이 살아 있는지 안 본다"
    assert "setInterval" in src.split("jog/status", 1)[0][-600:] or \
           "setInterval" in src.split("jog/status", 1)[1][:600], \
        "한 번만 확인한다 — 세션이 죽으면 그대로 굳는다"


def test_a_missing_joint_is_not_sent_as_zero():
    """⚠ 정규화 0 은 "가만히" 가 아니라 **가동범위 가운데**다. 안 온 관절을 0 으로
    채우면 팔이 크게 움직인다 — 자세를 잡는 창에서 그러면 위험하다."""
    src = code_only(_modal())
    send = src.split("const send = useCallback", 1)[1][:600]
    assert "?? 0" not in send, "빠진 관절을 0 으로 채운다"
