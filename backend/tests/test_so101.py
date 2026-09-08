"""SO-101 데몬 코어 — 외부 로봇이 기존 팔 계약에 타는 첫 사례 (feature/so101d.md).

여기서 지키는 결정들: 표준 7f 레코드 재사용, LeRobot 캘리브레이션 JSON 의
파일 호환(수식 동일·오독보다 거부), 6모터 전수 핑, lost 해제, 계약 동사.
"""

import json
from pathlib import Path

import pytest

from piper_so101 import calibration as cal_mod
from piper_so101.calibration import CalibrationError, MotorCal
from piper_so101.joints import MOTOR_IDS, SO101_JOINTS, from_record, to_record

REPO = Path(__file__).resolve().parents[2]
HUB_SRC = (REPO / "so101" / "piper_so101" / "hub.py").read_text()


def _cal(drive_mode: int = 0) -> dict[str, MotorCal]:
    return {n: MotorCal(id=MOTOR_IDS[n], drive_mode=drive_mode,
                        range_min=500, range_max=3500) for n in SO101_JOINTS}


def test_six_values_ride_the_seven_float_record():
    """관절 5+그리퍼가 표준 7f 레코드에 그대로 실린다 — joint6 은 0 으로
    채워진다. **빈 키가 없어야 한다**: StateWriter.publish 는 빠진 키를 조용히
    0 으로 채우지 않고 거부한다(0 은 정규화의 "가운데"라 명령으로 되돌아오면
    팔이 움직인다)."""
    rec = to_record({"shoulder_pan": 12.5, "gripper": 80.0})
    assert set(rec) == {"joint1", "joint2", "joint3", "joint4", "joint5",
                        "joint6", "gripper"}
    assert rec["joint1"] == 12.5 and rec["gripper"] == 80.0
    assert rec["joint6"] == 0.0
    back = from_record(rec)
    assert back["shoulder_pan"] == 12.5 and "joint6" not in back


def test_normalization_matches_the_lerobot_formula():
    """수식이 LeRobot `MotorsBus._normalize` 와 같아야 데이터셋이 호환된다:
    관절 = (클램프(raw)−min)/(max−min)×200−100, 그리퍼 = ×100,
    drive_mode 는 부호(그리퍼는 100−v) 반전. homing_offset 은 서보 EEPROM 에
    구워져 있어 여기서 적용하지 않는다."""
    cal = _cal()
    n = cal_mod.normalize({"shoulder_pan": 2000, "gripper": 2000}, cal)
    assert n["shoulder_pan"] == pytest.approx((1500 / 3000) * 200 - 100)
    assert n["gripper"] == pytest.approx(50.0)
    # 범위 밖은 클램프 — 캘리브레이션 밖 틱이 ±100 을 넘겨 나가면 안 된다
    n2 = cal_mod.normalize({"shoulder_pan": 9999}, cal)
    assert n2["shoulder_pan"] == 100.0
    # drive_mode 반전
    n3 = cal_mod.normalize({"shoulder_pan": 3500, "gripper": 3500}, _cal(drive_mode=1))
    assert n3["shoulder_pan"] == -100.0 and n3["gripper"] == 0.0


def test_denormalize_inverts_normalize():
    cal = _cal(drive_mode=1)
    for name, ticks in (("shoulder_pan", 613), ("gripper", 2711)):
        norm = cal_mod.normalize({name: ticks}, cal)
        assert cal_mod.denormalize(norm, cal)[name] == ticks


def test_an_unfamiliar_calibration_file_is_refused_not_guessed(tmp_path):
    """⚠ **오독보다 거부다.** lerobot-calibrate 포맷이 바뀌었는데 조용히 기본값으로
    떨어지면 "캘리브레이션 됐는데 팔이 이상하다"가 된다 — 여기서 제일 비싼
    오독. 빠진 모터·빠진 필드·바뀐 모터 ID·퇴화 범위 전부 명확히 실패한다."""
    good = {n: {"id": MOTOR_IDS[n], "drive_mode": 0, "homing_offset": 0,
                "range_min": 500, "range_max": 3500} for n in SO101_JOINTS}

    def _write(mutate) -> Path:
        data = json.loads(json.dumps(good))
        mutate(data)
        p = tmp_path / "cal.json"
        p.write_text(json.dumps(data))
        return p

    assert cal_mod.load_calibration(_write(lambda d: None))  # 정상은 통과
    for label, mutate in [
        ("모터 누락", lambda d: d.pop("wrist_roll")),
        ("필드 누락", lambda d: d["elbow_flex"].pop("range_min")),
        ("ID 불일치", lambda d: d["shoulder_pan"].update(id=9)),
        ("min==max", lambda d: d["gripper"].update(range_min=7, range_max=7)),
    ]:
        with pytest.raises(CalibrationError):
            cal_mod.load_calibration(_write(mutate))
        del label


def test_the_uncalibrated_fallback_is_marked_not_silent():
    """파일이 없으면 전범위 폴백으로 **발행은 되지만** calibrated=False 가
    같이 나간다 — 게이트웨이가 등록을 막는 근거다. 폴백이 조용하면 그 팔로
    모은 데이터셋은 그 팔 전용 쓰레기가 된다."""
    cal = cal_mod.default_calibration()
    assert cal["shoulder_pan"].range_min == 0
    assert cal["shoulder_pan"].range_max == 4095
    assert "calibrated" in HUB_SRC and "등록" in HUB_SRC


def test_attach_pings_all_six_and_names_the_cable():
    """⚠ **실측 (2026-09-08): 팔꿈치→손목 3핀 케이블 접촉 불량으로 4·5·6 이
    무응답이었다.** 조립 팔의 데이지체인은 케이블 하나로 끊긴다 — attach 는
    6모터 전수 핑을 하고, 빠진 ID 를 사람이 읽을 문구(케이블 힌트)로 말한다."""
    assert "ping_all" in HUB_SRC
    body = HUB_SRC.split("def attach", 1)[1].split("\n    def ", 1)[0]
    assert "팔꿈치→손목" in body, "실측 사고의 힌트 문구가 없다"
    assert "데이지체인" in body


def test_a_returning_adapter_clears_the_lost_mark():
    """rsd 카메라의 병(커밋 1b036f0)을 여기서는 처음부터 막는다 — 스캔에 다시
    보이면 lost 를 지운다. 안 지우면 스캔이 살린 팔을 감시가 2초 안에 도로
    없음 처리한다."""
    body = HUB_SRC.split("def scan", 1)[1].split("\n    def ", 1)[0]
    assert "lost_at = 0.0" in body, "돌아온 어댑터의 lost 를 안 지운다"


def test_the_daemon_speaks_the_contract_verbs():
    """데몬 계약 (feature/so101d.md §4.1): scan/attach/release/estop/info/lost.
    RPC 표면은 게이트웨이가 부르는 것만 — 임의 호출 창구가 아니다."""
    src = (REPO / "daemons" / "so101d.py").read_text()
    for verb in ("scan", "attach", "release", "estop", "info", "lost"):
        assert f'"{verb}"' in src, f"계약 동사 {verb} 가 없다"
    from piper_bus import contract as C
    assert C.SO101D in C.DAEMON_SOURCES, "heartbeat 자기 보고 대상에 없다"
    unit = (REPO / "deploy" / "systemd" / "piper-so101d.service").read_text()
    assert "Restart=always" in unit


def test_capabilities_tell_the_ui_what_buttons_to_draw():
    """UI 는 모델 문자열이 아니라 capabilities.features 로 분기한다 (§4.2) —
    마스터/슬레이브·영점굽기·슬립리셋은 SO-101 에 없다고 **선언**돼야
    카드에서 빠진다. 로봇 셋째부터 프론트 수술을 반복하지 않기 위한 계약이다."""
    body = HUB_SRC.split("def info", 1)[1].split("\n    def ", 1)[0]
    for key in ('"master_slave": False', '"hw_zero": False',
                '"slip_reset": False', '"joint_names"', '"dof": 5'):
        assert key in body, f"capabilities 에 {key} 가 없다"


def test_cleanup_touches_only_so101_segments():
    """robotd 의 팔 세그먼트를 지우면 그쪽 소비자가 깨진다 — sweep 은 자기
    접두사만 만진다 (camerad 가 dev_ 만 지우는 것과 같은 규칙)."""
    body = HUB_SRC.split("def sweep_stale", 1)[1]
    assert '".so101_"' in body


def test_deadman_stops_commands_but_keeps_torque():
    """정지 = 그 자리에 서기. 위치 제어라 보내기를 멈추면 마지막 목표에 선다 —
    토크를 끊으면 (가볍긴 해도) 쥔 것을 떨어뜨린다. 토크 OFF 는 E-stop 의
    몫이고, 스텝 클램프(MAX_STEP_TICKS)가 원거리 목표를 애초에 막는다."""
    assert "MAX_STEP_TICKS" in HUB_SRC
    dm = HUB_SRC.split("데드맨", 1)[1][:400]
    assert "set_torque" not in dm, "데드맨이 토크를 끊는다 — E-stop 과 역할이 섞였다"
    es = HUB_SRC.split("def estop", 1)[1].split("\n    def ", 1)[0]
    assert "set_torque" in es and "False" in es
