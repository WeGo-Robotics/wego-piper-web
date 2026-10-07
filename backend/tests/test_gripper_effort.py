"""그리퍼 힘(토크) — 수집·추론 화면 슬라이더 (2026-10-07).

Piper `GripperCtrl`(CAN 0x159) 은 **프레임마다** 위치와 힘을 같이 싣는다(0.001 N·m, 0~5000).
예전엔 두 곳에 `1000`(1.0 N·m)이 박혀 있었다. 이제 robotd 의 저장소 값을 싣는다 —
바꾸면 다음 프레임부터 그 힘이라 추론 도중에도 된다.
"""

import importlib
import inspect
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(tmp_path, monkeypatch):
    from piper_robot import gripper_store as G

    monkeypatch.setattr(G, "PATH", tmp_path / "gripper.json")
    monkeypatch.setattr(G, "_cache", None)
    return G


def test_the_default_is_the_force_every_arm_already_used(store):
    """이 기능을 넣었다고 이미 쓰던 팔의 잡는 힘이 바뀌면 안 된다 — 예전 값이 1000 이었다."""
    assert store.effort_mnm("can0") == 1000


def test_the_force_is_clamped_saved_per_arm_and_survives_a_restart(store):
    assert store.set_effort("can0", 2.5) == 2.5
    assert store.set_effort("can1", 99) == store.MAX_NM, "SDK 범위(5 N·m) 밖을 받아들였다"
    assert store.set_effort("can2", -1) == 0.0
    assert store.effort_mnm("can0") == 2500
    store._cache = None                                   # robotd 재시작
    assert store.effort_nm("can0") == 2.5 and store.effort_nm("can1") == store.MAX_NM
    assert store.effort_nm("can9") == store.DEFAULT_NM, "다른 팔까지 바뀌었다"


def test_no_command_path_still_hardcodes_the_force():
    """⚠ 한 곳만 고치면 그 경로에서만 슬라이더가 먹는다 — 파킹·명령 둘 다 저장소를 탄다."""
    for f in ("publish.py", "arm.py"):
        src = (REPO / "robot" / "piper_robot" / f).read_text()
        for line in src.splitlines():
            if "GripperCtrl(" in line and "0xAE" not in line:      # 0xAE 는 영점 굽기
                assert ", 1000," not in line, f"{f}: 힘이 아직 박혀 있다 — {line.strip()}"
        assert "gripper_store.effort_mnm(self.iface)" in src, f"{f} 가 저장소를 안 탄다"


def test_robotd_exposes_both_verbs():
    src = (REPO / "daemons" / "robotd.py").read_text()
    methods = src.split("_METHODS = {", 1)[1].split("}", 1)[0]
    assert '"get_gripper_effort"' in methods and '"set_gripper_effort"' in methods


def test_recording_refuses_a_change_and_inference_takes_it(monkeypatch):
    """⚠ 녹화 중에 바꾸면 한 데이터셋 안에서 잡는 힘이 달라지고, 그 차이는 프레임 어디에도
    안 남는다. 추론은 바꿔 보며 맞는 힘을 찾는 것이 목적이다(사용자 요청)."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import exclusivity as X
    from app.services import robot_manager as R

    sent = []
    monkeypatch.setattr(R, "_call", lambda m, *a, **k: sent.append((m, a)) or
                        {"iface": a[0], "effort_nm": a[1] if len(a) > 1 else 1.0,
                         "min_nm": 0, "max_nm": 5, "default_nm": 1})
    with TestClient(app) as c:
        monkeypatch.setattr(X, "is_running", lambda a: a == X.Activity.RECORDING)
        r = c.post("/api/robots/gripper-effort", json={"iface": "can0", "effort_nm": 2})
        assert r.status_code == 409 and "녹화" in r.json()["detail"]
        grip = [x for x in sent if x[0] == "set_gripper_effort"]
        assert not grip, "거절했는데 robotd 에 보냈다"

        monkeypatch.setattr(X, "is_running", lambda a: a == X.Activity.INFERENCE)
        r = c.post("/api/robots/gripper-effort", json={"iface": "can0", "effort_nm": 2})
        assert r.status_code == 200
        assert [x for x in sent if x[0] == "set_gripper_effort"] == [("set_gripper_effort", ("can0", 2.0))]


def test_both_screens_carry_the_slider():
    pages = REPO / "frontend" / "src" / "pages"
    rec = (pages / "RecordingPage.tsx").read_text()
    inf = (pages / "InferencePage.tsx").read_text()
    assert "<GripperEffortSlider iface={followerPort}" in rec
    # 추론은 시작 전 + 실행 중 두 곳
    assert inf.count("<GripperEffortSlider iface={selectedFollower}") == 2, \
        "추론 중에 바꿀 자리가 없다"
    comp = (REPO / "frontend" / "src" / "components" / "GripperEffortSlider.tsx").read_text()
    assert "/robots/gripper-effort" in comp
