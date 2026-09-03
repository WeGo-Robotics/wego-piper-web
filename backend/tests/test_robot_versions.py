"""버전 탭 — 팔별 펌웨어와 관절별 하드웨어 정보.

⚠ **관절별 펌웨어 버전은 없다.** Piper 프로토콜에 그런 필드가 없고 팔은 문자열
하나(`S-VX.X-X`)만 신고한다. 없는 것을 지어내면 화면이 거짓말을 하므로, 관절
마다는 실제로 읽히는 것(전압·온도·상태·설정 한계)만 낸다.
"""

import textwrap
from pathlib import Path

import pytest
from conftest import code_only, python_code_only
from fastapi.testclient import TestClient

from app.main import app
from piper_robot.arm import Arm

PANEL = (Path(__file__).resolve().parents[2] / "frontend" / "src"
         / "components" / "VersionPanel.tsx")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


ROW = {"iface": "can0", "firmware": "S-V1.8-2", "master_slave": "slave",
       "ctrl_mode": "Standby", "sdk": "0.6.1", "protocol": "V2",
       "interface": "V2",
       "joints": [{"joint": "joint1", "motor": 1, "voltage_v": 23.0,
                   "driver_temp_c": 35, "motor_temp_c": 29, "enabled": True,
                   "flags": [], "angle_min_deg": -150.0, "angle_max_deg": 150.0,
                   "max_spd_rad_s": 0.3, "max_acc_rad_s2": 5.0}]}


def test_it_lists_the_arm_firmware_and_every_joint(client, monkeypatch):
    from app.services import robot_manager as rm

    monkeypatch.setattr(rm, "_call", lambda m, *a, **k: [ROW] if m == "versions" else None)
    got = client.get("/api/robots/versions")
    assert got.status_code == 200, got.text
    arm = got.json()["arms"][0]
    assert arm["firmware"] == "S-V1.8-2"
    j = arm["joints"][0]
    assert j["angle_min_deg"] == -150.0 and j["max_acc_rad_s2"] == 5.0


def test_a_dead_daemon_is_said_out_loud(client, monkeypatch):
    """⚠ 못 읽는 것과 팔이 없는 것은 다르다 — 빈 목록을 내면 "팔이 없다" 로 읽힌다."""
    from app.services import robot_manager as rm

    monkeypatch.setattr(rm, "_call", lambda m, *a, **k: None)
    got = client.get("/api/robots/versions")
    assert got.status_code == 503 and "robotd" in got.json()["detail"]


def test_a_failed_read_is_none_not_zero():
    """⚠ 읽기 실패를 0 으로 채우면 **정상값처럼 보인다** — 0V·0℃ 는 그럴듯한
    숫자라 사람이 못 거른다. 모르면 None 이고 화면은 `—` 로 그린다."""
    assert Arm._safe(lambda: 1 / 0) is None
    assert Arm._safe(lambda: 7) == 7

    src = code_only(PANEL.read_text())
    assert "'—'" in src, "모르는 값을 숫자로 그린다"


def test_a_joint_is_found_by_its_number_not_by_list_order():
    """⚠ 리스트 순서가 모터 번호와 같다고 가정하면, 응답이 덜 왔을 때 **남의
    값을 그 관절의 값으로** 보여준다."""
    class _R:
        def __init__(self, num, val):
            self.motor_num, self.max_angle_limit = num, val
            self.min_angle_limit, self.max_joint_spd = 0, 0

    class _Wrap:
        def __init__(self, rows):
            self.all_motor_angle_limit_max_spd = type("A", (), {"motor": rows})()

    rows = [_R(3, 300), _R(1, 100)]              # 순서가 뒤섞여 있다
    got = Arm._pick(_Wrap(rows), "all_motor_angle_limit_max_spd", 1, "motor_num")
    assert got is not None and got.max_angle_limit == 100, "순서로 집었다"


def test_the_screen_says_there_is_no_per_joint_firmware():
    """⚠ 빈 칸을 만들어 두면 사람이 "왜 안 나오지" 를 묻게 된다. 없는 이유를
    화면이 말해야 한다 — 그게 없는 것과 못 읽은 것을 가른다."""
    text = PANEL.read_text()
    assert "관절별 펌웨어 버전은 없습니다" in text
    assert "S-VX.X-X" in text


def test_it_does_not_poll():
    """⚠ 버전은 안 변하고, 이 조회는 팔마다 한계값을 **물어보는**
    (Search→대기→Get) 일이라 주기적으로 돌릴 성질이 아니다."""
    src = code_only(PANEL.read_text())
    assert "setInterval" not in src, "버전 화면이 폴링한다"

    import inspect

    from app.routers.robots import robot_versions

    api_src = python_code_only(textwrap.dedent(inspect.getsource(robot_versions)))
    assert "timeout=15" in api_src, "느린 조회인데 기본 타임아웃을 쓴다"


def test_a_master_arms_zeros_are_reported_as_missing_not_as_readings():
    """⚠ **0V 는 읽은 값이 아니라 "안 왔다" 이다.** 마스터(示教输入臂)는 저속
    피드백도 안 보내서 전부 0 으로 얼어 있는데, 그대로 그리면 `0.0V·0℃` 가
    측정값처럼 보인다 — 전원이 들어온 팔은 23V 다. 실기에서 can1·can3 이 그렇게
    나왔다."""
    class _D:
        def __init__(self, vol):
            self.vol, self.foc_temp, self.motor_temp = vol, 0, 0
            self.foc_status = type("F", (), {"driver_enable_status": False})()

    dead = Arm._driver_row(type("S", (), {"motor_1": _D(0)})(), 1)
    assert dead == {"feedback": False}, dead
    assert "voltage_v" not in dead, "안 온 값을 0 으로 그린다"

    live = Arm._driver_row(type("S", (), {"motor_1": _D(230)})(), 1)
    assert live["feedback"] is True and live["voltage_v"] == 23.0

    src = code_only(PANEL.read_text())
    assert "j.feedback === false" in src, "화면이 '피드백 없음' 을 구분하지 않는다"
