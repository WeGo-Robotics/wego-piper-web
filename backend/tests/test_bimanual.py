"""양팔(bimanual) 배선 계약 (feature/bimanual.md).

로봇 클래스 자체(vendor bi_piper_*)는 draccus 파싱·팩토리 스모크로 따로 확인했고,
여기는 **백엔드가 만드는 인자와 검증**을 고정한다 — 여기가 갈리면 draccus
안쪽에서 죽어 원인을 알기 어렵다.
"""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core import cli_mapping
from app.core.cli_mapping import build_inference_args, build_record_args
from app.main import app
from app.routers.recording import _apply_arm_params, _split_camera_mapping
from app.services.robot_manager import ArmInfo, RobotManager


@pytest.fixture
def client():
    return TestClient(app)


# ── CLI 인자 조립 ──


def test_record_args_bimanual_nested(monkeypatch):
    monkeypatch.setattr(cli_mapping.settings, "robot_transport", "shm")
    args = build_record_args({
        "robot_type": "bi_piper_follower",
        "teleop_type": "bi_piper_leader",
        "left_robot_port": "can_follower1", "right_robot_port": "can_follower2",
        "left_teleop_port": "can_leader1", "right_teleop_port": "can_leader2",
        "left_robot_cameras": {"top": {"type": "shm", "segment": "s", "width": 640, "height": 480, "fps": 30}},
        "repo_id": "u/bi", "single_task": "handover",
    })
    joined = " ".join(args)
    # shm 전송이면 bi 타입도 _shm 으로 승격돼야 한다
    assert "--robot.type=bi_piper_follower_shm" in joined
    assert "--teleop.type=bi_piper_leader_shm" in joined
    assert "--robot.left_arm_config.port=can_follower1" in joined
    assert "--robot.right_arm_config.port=can_follower2" in joined
    assert "--teleop.left_arm_config.port=can_leader1" in joined
    assert "--teleop.right_arm_config.port=can_leader2" in joined
    assert '--robot.left_arm_config.cameras={"top"' in joined
    # 단수 키가 섞이면 draccus 가 bi 설정에서 죽는다
    assert "--robot.port=" not in joined
    assert "--teleop.port=" not in joined


def test_inference_args_bimanual_ports():
    args = build_inference_args({
        "checkpoint_path": "/m", "robot_type": "piper_follower_shm",
        "robot_port": "can_follower1",
        "robot_ports": ["can_follower1", "can_follower2"],
    })
    i = args.index("--robot-ports")
    assert args[i + 1] == "can_follower1,can_follower2"


def test_apply_arm_params_strips_scalar_keys(monkeypatch):
    monkeypatch.setattr(
        "app.services.camera_config.build_cameras_json",
        lambda m, width, height, fps: {k: {"type": "shm"} for k in m},
    )
    params = _apply_arm_params({
        "robot_port": "canX", "teleop_port": "canY",
        "robot_ports": ["can_follower1", "can_follower2"],
        "teleop_ports": ["can_leader1", "can_leader2"],
        "camera_mapping": {"top": "c1", "left_hand": "c2", "right_hand": "c3"},
    }, cam_w=640, cam_h=480, cam_fps=30)
    assert "robot_port" not in params and "teleop_port" not in params
    assert params["left_robot_port"] == "can_follower1"
    assert params["right_teleop_port"] == "can_leader2"
    # 접두사 배정: left_hand→왼팔 hand, right_hand→오른팔 hand, top(공용)→왼팔
    assert set(params["left_robot_cameras"]) == {"top", "hand"}
    assert set(params["right_robot_cameras"]) == {"hand"}


def test_split_camera_mapping_convention():
    left, right = _split_camera_mapping({"top": "a", "left_wrist": "b", "right_wrist": "c"})
    assert left == {"top": "a", "wrist": "b"}
    assert right == {"wrist": "c"}


# ── 녹화 시작 검증 ──


def test_record_start_rejects_bimanual_with_single_type(client):
    r = client.post("/api/recording/start", json={
        "robot_type": "piper_follower",
        "robot_ports": ["can_follower1", "can_follower2"],
        "teleop_ports": ["can_leader1", "can_leader2"],
        "repo_id": "u/d", "single_task": "t",
    })
    assert r.status_code == 400 and "bi 로봇 타입" in r.json()["detail"]


def test_record_start_rejects_missing_second_leader(client):
    r = client.post("/api/recording/start", json={
        "robot_type": "bi_piper_follower",
        "robot_ports": ["can_follower1", "can_follower2"],
        "teleop_ports": ["can_leader1"],
        "repo_id": "u/d", "single_task": "t",
    })
    assert r.status_code == 400 and "Leader 포트 2개" in r.json()["detail"]


# ── 관절 수 검증 (14축 개방) ──


def test_validate_bimanual_reports_14_joints(client, monkeypatch):
    from app.routers import inference as inf
    from app.services import robot_manager as rm

    monkeypatch.setattr(inf, "get_model", lambda mid: {
        "requirements": {"required_cameras": [], "state_dim": 14, "action_dim": 14},
    })
    arms = {}
    for iface in ("can_follower1", "can_follower2"):
        a = ArmInfo(iface=iface)
        a.role, a.ready = "follower", True
        arms[iface] = a
    monkeypatch.setattr(rm.robot_manager, "arms", arms)

    r = client.post("/api/inference/validate", json={
        "model_id": "m", "follower_iface": "can_follower1",
        "follower_ifaces": ["can_follower1", "can_follower2"],
        "camera_mapping": {},
    })
    body = r.json()
    assert body["robot_joints"] == 14
    assert body["valid"], body["errors"]  # 14축 모델이 7 에 막히면 양팔 추론 전체가 시작 불가


def test_validate_single_still_7(client, monkeypatch):
    from app.routers import inference as inf
    from app.services import robot_manager as rm

    monkeypatch.setattr(inf, "get_model", lambda mid: {
        "requirements": {"required_cameras": [], "state_dim": 7, "action_dim": 7},
    })
    a = ArmInfo(iface="can1")
    a.role, a.ready = "follower", True
    monkeypatch.setattr(rm.robot_manager, "arms", {"can1": a})
    r = client.post("/api/inference/validate", json={
        "model_id": "m", "follower_iface": "can1", "camera_mapping": {},
    })
    assert r.json()["robot_joints"] == 7


# ── 좌/우 등록 ──


def test_slot_assignment_defaults_side():
    m = RobotManager()
    for iface in ("canA", "canB"):
        m.arms[iface] = ArmInfo(iface=iface)
    m.assign_slot("canA", "follower_1")
    m.assign_slot("canB", "follower_2")
    assert m.arms["canA"].side == "left"
    assert m.arms["canB"].side == "right"
    # 스왑은 등록에 남는다
    m.set_side("canA", "right")
    assert m.arms["canA"].side == "right"
    assert m.arms["canA"].to_dict()["side"] == "right"
    # 역할이 바뀌면 side 도 무효 (슬롯 페어에 붙는 해석이므로)
    m.set_role("canA", "leader")
    assert m.arms["canA"].side is None


# ── phase 분석기 14축 ──


def test_phase_signals_use_active_gripper_on_14_axes():
    from piper_phase.fsm import Params, compute_signals

    T = 40
    state = np.zeros((T, 14))
    action = np.zeros((T, 14))
    # 오른팔 그리퍼(인덱스 13)만 활동: 열림(80)→닫힘(10). 왼팔 그리퍼(6)는 죽어 있음
    state[:, 13] = np.linspace(80, 10, T)
    action[:, 13] = 0.0
    sig = compute_signals(state, action, Params())
    # 예전에는 (T,14) 를 받아도 인덱스 6(왼팔 그리퍼=0)을 그리퍼로 읽어
    # 소리 없이 왼팔만 분석했다 — 활동 그리퍼(오른팔)를 골라야 한다
    assert sig.gripper_state[0] == pytest.approx(80.0)
    assert sig.gripper_state[-1] == pytest.approx(10.0)


def test_phase_signals_single_arm_unchanged():
    from piper_phase.fsm import Params, compute_signals

    T = 20
    state = np.zeros((T, 7))
    action = np.zeros((T, 7))
    state[:, 6] = 50.0
    sig = compute_signals(state, action, Params())
    assert sig.gripper_state[0] == pytest.approx(50.0)
