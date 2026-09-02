"""로봇 전체 초기화 — 사용 가능·1단계·2단계를 한 번에 비운다.

⚠ **메모리만 비우면 거짓말이 된다.** 게이트웨이가 재시작하면
`restore_session()` 이 세션 파일을 읽어 스캔·연결·역할까지 되살린다.
"지웠는데 재부팅하니 돌아왔다" 는 안 지운 것보다 나쁘다.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import robot_manager as rm


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def two_arms(tmp_path, monkeypatch):
    """등록된 팔 하나 + 연결만 된 팔 하나 — 세 목록이 각각 차 있는 상태."""
    session = tmp_path / "robot_session.json"
    monkeypatch.setattr(rm, "ROBOT_SESSION_PATH", session)
    monkeypatch.setattr(rm, "SESSION_DIR", tmp_path)

    mgr = rm.robot_manager
    saved = dict(mgr.arms), mgr.selected_type, mgr.config_name
    mgr.arms = {}
    for iface, role, ready in (("can_f1", "follower", True), ("can_l1", "leader", False)):
        arm = rm.ArmInfo(iface=iface)
        arm.connected, arm.role, arm.ready = True, role, ready
        # 실기 CAN 을 건드리지 않는다 — 끊겼는지만 본다
        monkeypatch.setattr(arm, "disconnect", lambda a=arm: setattr(a, "connected", False))
        mgr.arms[iface] = arm
    mgr.save_session()
    yield mgr, session
    mgr.arms, mgr.selected_type, mgr.config_name = saved


def test_clearing_empties_all_three_lists(client, two_arms):
    mgr, _ = two_arms
    assert mgr.get_ready_arms(), "준비: 등록된 팔이 있어야 한다"

    r = client.post("/api/robots/clear")
    assert r.status_code == 200, r.text
    assert r.json()["cleared"] == 2
    assert sorted(r.json()["disconnected"]) == ["can_f1", "can_l1"]

    assert mgr.arms == {}, "1·2단계 목록의 원천이 안 비었다"
    assert mgr.get_ready_arms() == [], "사용 가능 목록이 안 비었다"


def test_the_session_file_goes_too(client, two_arms):
    """⚠ 이게 없으면 게이트웨이 재시작이 지운 팔을 되살린다."""
    mgr, session = two_arms
    assert session.exists(), "준비: 세션이 저장돼 있어야 한다"

    client.post("/api/robots/clear")

    assert not session.exists(), "세션이 남아 재시작하면 되살아난다"
    assert mgr.restore_session() is False, "복원이 여전히 팔을 되살린다"


def test_it_refuses_while_the_arm_is_moving(client, two_arms, monkeypatch):
    """⚠ 연결을 끊으면 토크가 빠진다 — 추론·녹화·텔레옵 중이면 팔이 주저앉고,
    그 활동도 팔을 잃는다. `/zero` 와 같은 판단이다."""
    from app.services import exclusivity

    mgr, session = two_arms
    monkeypatch.setattr(exclusivity, "running", lambda: [exclusivity.Activity.INFERENCE])

    r = client.post("/api/robots/clear")
    assert r.status_code == 409, r.text
    assert "추론" in r.json()["detail"]
    assert mgr.arms, "거절했는데 팔이 지워졌다"
    assert session.exists(), "거절했는데 세션이 지워졌다"


def test_the_button_hides_when_there_is_nothing_to_clear():
    """⚠ 이미 깨끗한데 "초기화" 를 권하면 누를 이유를 만들어 주는 셈이다.
    위험한 버튼일수록 그러면 안 된다."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2]
           / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    assert "{arms.length > 0 && (" in src, "빈 상태에서도 위험 버튼이 보인다"
    assert "토크가 빠져 팔이 주저앉습니다" in src, "확인창이 토크를 경고하지 않는다"
