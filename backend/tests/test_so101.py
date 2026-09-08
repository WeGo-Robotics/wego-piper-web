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


# ── 게이트웨이 통로 (3단계) ──


def test_the_web_reaches_so101_only_through_the_hub_client():
    """웹은 데몬을 직접 모른다 (docs/robot-daemon-contract.md) — 라우터·감시는
    so101_client 만 부르고, 클라이언트는 죽은 데몬을 기다리지 않는다
    (is_alive 단축 — rsd 에서 스캔이 분 단위로 멈춘 실측의 재발 방지)."""
    client = (REPO / "backend" / "app" / "services" / "so101_client.py").read_text()
    assert "is_alive(C.SO101D)" in client
    router = (REPO / "backend" / "app" / "routers" / "robots.py").read_text()
    assert "so101_client" in router
    assert "scservo" not in router, "라우터가 모터 SDK 를 직접 만진다"


def test_serial_ports_ride_the_ports_route_as_their_own_kind():
    """시리얼 카드는 CAN 카드와 **종류가 다르다** — UP/DOWN·Rx/Tx 가 없는
    장치를 그 틀에 욱여넣지 않는다. /ports 응답의 별도 목록(serial)로 나가고,
    데몬이 없으면 빈 목록이라 화면이 패널을 접는다."""
    router = (REPO / "backend" / "app" / "routers" / "robots.py").read_text()
    body = router.split('"/ports"', 1)[1].split("@router.post", 1)[0]
    assert '"serial": serial' in body
    page = (REPO / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    assert "ports.length + serialPorts.length === 0" in page, "시리얼만 있을 때 빈 상태로 보인다"
    assert "미캘리브레이션" in page, "캘리브레이션 없는 팔이 표시가 안 된다"


def test_attach_failures_carry_the_daemons_words():
    """attach 는 사람이 누른 버튼이다 — 데몬이 만든 사유(케이블 힌트·
    캘리브레이션 거부)가 400 으로 **그대로** 화면까지 간다. 게이트웨이가
    문구를 새로 지으면 힌트가 사라진다."""
    router = (REPO / "backend" / "app" / "routers" / "robots.py").read_text()
    body = router.split('"/serial/attach"', 1)[1].split("@router.post", 1)[0]
    assert "HTTPException(400, str(exc))" in body


def test_so101_lost_joins_the_device_alert_flow():
    """so101d 의 lost 는 그대로 중계한다 — 브리지(=attach 된 팔)만 보고하므로
    전부 "쓰려던 팔"이다. robotd 처럼 등록부 대조가 필요 없는 이유를 여기
    박아 둔다 (robotd 는 전체 초기화 뒤에도 lost 를 보고해 대조가 필수였다)."""
    watch = (REPO / "backend" / "app" / "services" / "device_watch.py").read_text()
    assert "so101_client.lost()" in watch


def test_the_contract_doc_names_the_verbs_and_channels():
    """계약 문서가 곧 다음 로봇의 체크리스트다 — 동사·채널·capabilities 가
    문서에 있어야 로봇 셋째가 게이트웨이 수술 없이 붙는다."""
    doc = (REPO / "docs" / "robot-daemon-contract.md").read_text()
    for must in ("scan", "attach", "release", "estop", "info", "lost",
                 "capabilities", "deadman", "heartbeat", "features"):
        assert must in doc, f"계약 문서에 {must} 가 없다"


# ── 캘리브레이션 위저드 ──


class _FakeBus:
    """모터 없는 시험용 버스 — EEPROM 쓰기를 기록만 한다."""

    def __init__(self):
        self.homings: dict[int, int] = {}
        self.limits: dict[int, tuple[int, int]] = {}
        self.positions = {i: 2000 + i * 100 for i in range(1, 7)}

    def set_torque(self, mid, on):
        return True

    def unlock_eeprom(self, mid):
        return True

    def write_homing(self, mid, off):
        self.homings[mid] = off
        return True

    def write_limits(self, mid, mn, mx):
        self.limits[mid] = (mn, mx)
        return True

    def sync_read_positions(self):
        return dict(self.positions)


def _wizard_hub():
    from piper_so101.hub import So101Bridge, So101Hub

    hub = So101Hub()
    b = So101Bridge("so101_leader1", _FakeBus(), cal_mod.default_calibration(),
                    calibrated=False)
    b._running = True                      # 스레드 없이 상태기계만 검증한다
    hub.bridges["so101_leader1"] = b
    return hub, b


def test_the_center_is_computed_from_the_sweep_not_posed(monkeypatch, tmp_path):
    """**중앙 자세를 사람이 잡지 않는다** — 양 끝까지 훑으면 중앙은
    (min+max)/2 다. 눈대중보다 정확하고 단계도 하나 준다. begin 은 바로
    범위 스윕으로 가고(공장 초기화 후), 호밍은 저장 때 중점−2047 로 굽는다."""
    monkeypatch.setenv("PIPER_SO101_CALIB_DIR", str(tmp_path))
    hub, b = _wizard_hub()
    st = hub.calib_begin("so101_leader1")
    assert st["stage"] == "range", "중앙 단계가 아직 남아 있다"
    assert b.bus.homings == {i: 0 for i in range(1, 7)}, "begin 이 공장 초기화를 안 한다"
    for n in SO101_JOINTS:
        b._calib_min[n], b._calib_max[n] = 500, 3500
    hub.calib_save("so101_leader1")
    assert b.bus.homings[1] == (500 + 3500) // 2 - 2047     # 중점 2000 → −47
    assert b.bus.limits[1] == (2047 - 1500, 2047 + 1500)    # 2047 중심으로 이사


def test_a_rollover_joint_still_sweeps_contiguously():
    """0/4095 롤오버를 걸치는 조립도 min/max 가 안 찢어진다 — 60Hz 견본에서
    2048 틱 넘는 점프는 사람 손이 아니라 롤오버다. 언랩으로 이어붙인다."""
    from piper_so101.hub import _unwrap

    seq = [4000, 4090, 10, 80]              # 4095 를 넘어간다
    uw, last = None, None
    out = []
    for raw in seq:
        uw = _unwrap(raw, last, uw)
        last = raw
        out.append(uw)
    assert out == [4000, 4090, 4106, 4176], "롤오버에서 min/max 가 찢어진다"


def test_save_refuses_joints_that_did_not_move(monkeypatch, tmp_path):
    """⚠ 안 움직인 관절로 저장하면 범위가 퇴화해 그 팔의 정규화가 통째로
    깨진다 — 어느 관절이 부족한지 이름으로 말하고 거부한다. 문구가 곧 화면이다."""
    from piper_so101.hub import So101Error

    monkeypatch.setenv("PIPER_SO101_CALIB_DIR", str(tmp_path))
    hub, b = _wizard_hub()
    hub.calib_begin("so101_leader1")
    for n in SO101_JOINTS:
        b._calib_min[n], b._calib_max[n] = 500, 3500
    b._calib_min["wrist_roll"], b._calib_max["wrist_roll"] = 2000, 2050   # 부족
    with pytest.raises(So101Error) as e:
        hub.calib_save("so101_leader1")
    assert "wrist_roll" in str(e.value)


def test_save_writes_a_lerobot_compatible_file_and_reloads(monkeypatch, tmp_path):
    """저장 = 서보 리밋 굽기 + LeRobot 포맷 JSON + 즉시 재적용(calibrated=True).
    저장한 파일을 **우리 리더가 도로 읽을 수 있는지**까지가 계약이다."""
    monkeypatch.setenv("PIPER_SO101_CALIB_DIR", str(tmp_path))
    hub, b = _wizard_hub()
    hub.calib_begin("so101_leader1")
    for n in SO101_JOINTS:
        b._calib_min[n], b._calib_max[n] = 500, 3500
    st = hub.calib_save("so101_leader1")
    assert st["stage"] is None and st["calibrated"] is True
    saved = json.loads((tmp_path / "so101_leader1.json").read_text())
    assert saved["shoulder_pan"]["homing_offset"] == -47      # 중점 2000 − 2047
    assert saved["shoulder_pan"]["range_min"] == 547          # 2047 − 1500
    assert cal_mod.load_calibration(tmp_path / "so101_leader1.json")
    assert b.cal["shoulder_pan"].range_min == 547, "새 캘리브레이션이 즉시 안 탄다"


def test_homing_register_uses_sign_magnitude_bit_11():
    """Feetech Homing_Offset 은 2의 보수가 아니라 **부호-크기(비트 11)**다 —
    음수를 그냥 쓰면 (예: −53 → 65483) 팔이 반바퀴 틀어진 기준을 갖게 된다."""
    from piper_so101.feetech import encode_sign_magnitude

    assert encode_sign_magnitude(53, 11) == 53
    assert encode_sign_magnitude(-53, 11) == 53 | (1 << 11)
    with pytest.raises(ValueError):
        encode_sign_magnitude(5000, 11)


def test_the_wizard_ui_polls_and_never_double_begins():
    """위저드는 그리기만 한다 — 판정(스팬)은 데몬 몫. StrictMode 이중 마운트에
    begin 을 두 번 치면 첫 호밍이 0 으로 되돌아가므로 가드가 계약이다."""
    src = (REPO / "frontend" / "src" / "components" /
           "So101CalibrationWizard.tsx").read_text()
    assert "started.current" in src, "이중 begin 가드가 없다"
    assert "calib/status" in src
    page = (REPO / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    assert "So101CalibrationWizard" in page
    router = (REPO / "backend" / "app" / "routers" / "robots.py").read_text()
    assert 'Literal["begin", "save", "cancel"]' in router, \
        "단계가 자유 문자열이면 데몬 RPC 임의 호출 창구가 된다"


# ── 재연결 (케이블 뽑았다 꽂기) ──


def test_replugging_resurrects_the_arm_instead_of_farming_bridges(monkeypatch, tmp_path):
    """⚠ **실측 (2026-09-08): [연결]을 누를 때마다 leader2..11 이 생겨 브리지
    11개가 한 포트를 두고 싸웠다** (multiple access 예외). 죽은 브리지가
    이름과 스캔 매핑을 점유한 채 안 치워졌기 때문이다. attach 는 같은
    어댑터의 죽은 브리지를 치우고 **같은 이름으로 부활**시킨다 — 재연결은
    새 팔이 아니다. 살아 있으면 거부한다 — 어댑터당 브리지는 하나다."""
    from piper_so101 import hub as hub_mod

    (tmp_path / "usb-1a86_TEST-if00").write_text("")
    monkeypatch.setenv("PIPER_SO101_CALIB_DIR", str(tmp_path / "cal"))
    monkeypatch.setattr(hub_mod, "_BY_ID", tmp_path)

    class _AttachBus(_FakeBus):
        def __init__(self, port):
            super().__init__()
            self.port_name = port

        def ping_all(self, ids):
            return {i: 777 for i in ids}

    monkeypatch.setattr(hub_mod, "FeetechBus", _AttachBus)
    # 스레드·실제 shm 세그먼트 없이 attach 논리만 검증한다 — 진짜 so101d 가
    # 같은 세그먼트 이름을 발행 중일 수 있어 StateWriter 를 열면 안 된다
    monkeypatch.setattr(hub_mod.So101Bridge, "start",
                        lambda self: setattr(self, "_running", True))
    monkeypatch.setattr(hub_mod.So101Bridge, "stop",
                        lambda self: setattr(self, "_running", False))

    hub = hub_mod.So101Hub()
    a1 = hub.attach("usb-1a86_TEST-if00", "")
    assert a1["arm"] == "so101_leader1"
    with pytest.raises(hub_mod.So101Error):        # 살아 있으면 거부
        hub.attach("usb-1a86_TEST-if00", "")
    hub.bridges["so101_leader1"]._running = False   # 케이블 뽑힘 = lost
    a2 = hub.attach("usb-1a86_TEST-if00", "")
    assert a2["arm"] == "so101_leader1", "재연결이 새 이름을 만든다"
    assert list(hub.bridges) == ["so101_leader1"], "죽은 브리지가 안 치워졌다"


def test_a_lost_bridge_releases_the_serial_port():
    """죽은 브리지가 포트 fd 를 물고 있으면 재연결 attach 와 같은 포트를 두고
    싸운다 — _declare_lost 는 세그먼트와 함께 포트도 놓는다."""
    body = HUB_SRC.split("def _declare_lost", 1)[1].split("\n    def ", 1)[0]
    assert "self.bus.close()" in body


def test_eeprom_writes_pause_the_reader_and_retry():
    """⚠ **실기: 저장을 여러 번 눌러야 했다.** 60Hz sync read 와 EEPROM 쓰기가
    같은 시리얼에서 엉키면 쓰기 응답을 놓친다 — 쓰기 구간은 읽기 루프를
    쉬게 하고(io_pause), 쓰기 자체도 3회 재시도한다. 핫패스(write_goal)는
    재시도 없음 — 다음 프레임이 곧 재시도다."""
    feetech = (REPO / "so101" / "piper_so101" / "feetech.py").read_text()
    body = feetech.split("def _write2", 1)[1].split("\n    def ", 1)[0]
    assert "range(3)" in body, "EEPROM 쓰기 재시도가 없다"
    goal = feetech.split("def write_goal", 1)[1].split("\n    # ", 1)[0]
    assert "range(3)" not in goal, "핫패스에 재시도가 붙었다"
    hub = (REPO / "so101" / "piper_so101" / "hub.py").read_text()
    assert hub.count("b.io_pause = True") >= 2, "begin/save 가 읽기를 안 쉰다"
    assert "if self.io_pause:" in hub


def test_the_sweep_is_a_circle_not_a_bar():
    """⚠ **관절은 원이다.** 직선 막대는 실기에서 두 번 실패했다: 언랩 좌표가
    화면을 뚫었고, 가동범위가 0/4095 를 걸치는 관절(4096→0 반대 범위)에서
    마커가 끝에서 끝으로 점프했다. 원형 게이지는 어느 방향으로 돌든, 어디서
    0 을 지나든 호가 그냥 이어진다 — 방향 반전도 표시 문제가 아니게 된다."""
    src = (REPO / "frontend" / "src" / "components" /
           "So101CalibrationWizard.tsx").read_text()
    assert "arcPath" in src and "<svg" in src, "원형 게이지가 아니다"
    assert "% 4096" in src, "틱→각도가 모듈로가 아니다 — 롤오버에서 깨진다"
    assert "359.9" in src, "한 바퀴 호의 퇴화(arc 명령 한계)를 안 다룬다"
    assert "직선" not in src.split("function JointGauge")[1].split("}")[0]


def test_the_side_survives_replug_and_daemon_restart(monkeypatch, tmp_path):
    """좌/우는 텔레옵 짝짓기의 재료다 — 팔의 정체는 어댑터(by_id)이므로 그
    열쇠로 데몬 세션에 남고, 재연결(부활)이 복원한다."""
    from piper_so101 import hub as hub_mod

    (tmp_path / "usb-1a86_TEST-if00").write_text("")
    monkeypatch.setenv("PIPER_SO101_CALIB_DIR", str(tmp_path / "cal"))
    monkeypatch.setattr(hub_mod, "_BY_ID", tmp_path)
    monkeypatch.setattr(hub_mod, "_SESSION_PATH", tmp_path / "session.json")

    class _AttachBus(_FakeBus):
        def __init__(self, port):
            super().__init__()
            self.port_name = port

        def ping_all(self, ids):
            return {i: 777 for i in ids}

    monkeypatch.setattr(hub_mod, "FeetechBus", _AttachBus)
    monkeypatch.setattr(hub_mod.So101Bridge, "start",
                        lambda self: setattr(self, "_running", True))
    monkeypatch.setattr(hub_mod.So101Bridge, "stop",
                        lambda self: setattr(self, "_running", False))

    hub = hub_mod.So101Hub()
    hub.attach("usb-1a86_TEST-if00", "")
    st = hub.set_side("so101_leader1", "left")
    assert st["side"] == "left"
    # 재연결 (부활)
    hub.bridges["so101_leader1"]._running = False
    assert hub.attach("usb-1a86_TEST-if00", "")["side"] == "left"
    # 데몬 재기동 (새 허브가 세션을 읽는다)
    hub2 = hub_mod.So101Hub()
    assert hub2.attach("usb-1a86_TEST-if00", "")["side"] == "left"


# ── 릴레이 (4단계) — 관절 매칭·POSE 정합의 순수 계산 ──


def test_norm_to_radians_uses_the_calibrated_span():
    """정규화 v 의 각도는 캘리브레이션 폭이 정한다: (v/200)·span·(2π/4096).
    정규화 공간에서 그대로 매핑하면 두 팔의 범위 차이만큼 각도가 왜곡된다 —
    Piper 마스터 그리퍼 사고와 같은 병이라 각도 공간이 정본이다."""
    import math

    from piper_so101 import relay_map

    spans = {n: 2048.0 for n in SO101_JOINTS}       # 반바퀴 스윕
    rad = relay_map.leader_rad({"joint1": 100.0}, spans)
    # +100 = 중앙에서 폭의 절반(1024틱) = 1024/4096 바퀴 = π/2
    assert rad["shoulder_pan"] == pytest.approx(math.pi / 2)
    assert "gripper" not in rad, "그리퍼는 각도가 아니다 — 절대 통과"


def test_the_joint_map_moves_deltas_and_freezes_the_forearm():
    """관절 매칭의 본식: 팔로워 = 앵커 + 부호×(리더 변화량). 영점 규약·시작
    자세 차이는 앵커가 지운다 (ACT-delta 와 같은 산수). 대응 없는 Piper
    joint4(전완 롤)는 **정합 시점 값 유지** — 0 강제가 아니다."""
    import numpy as np

    from piper_so101 import relay_map

    f_anchor = np.array([0.1, 0.2, 0.3, 0.7, 0.5, 0.6])
    l_anchor = {n: 0.0 for n in SO101_JOINTS if n != "gripper"}
    lead = dict(l_anchor, shoulder_pan=0.25, wrist_roll=-0.1)
    goal = relay_map.map_joint_goal(lead, l_anchor, f_anchor)
    assert goal[0] == pytest.approx(0.1 + 0.25)     # pan → joint1
    assert goal[5] == pytest.approx(0.6 - 0.1)      # wrist_roll → joint6
    assert goal[3] == pytest.approx(0.7), "joint4 가 앵커를 안 지킨다"
    # 리더가 앵커 그대로면 팔로워도 앵커 그대로 — 정합 순간 점프 0 의 근거
    same = relay_map.map_joint_goal(l_anchor, l_anchor, f_anchor)
    assert np.allclose(same, f_anchor)


def test_the_relative_pose_target_is_identity_at_engage():
    """POSE 정합의 본식: 목표 = T_f0·(T_l0⁻¹·T_l). 정합 순간(T_l == T_l0)의
    목표가 곧 팔로워 정합 자세다 — 기존 절대 POSE 의 첫 프레임 점프가 여기엔
    구조적으로 없다."""
    import numpy as np

    from piper_so101 import relay_map

    rng = np.random.default_rng(7)

    def _rot(ax: int, a: float) -> "np.ndarray":
        c, s_ = np.cos(a), np.sin(a)
        m = np.eye(3)
        i, j = [(1, 2), (0, 2), (0, 1)][ax]
        m[i, i] = m[j, j] = c
        m[i, j], m[j, i] = -s_, s_
        return m

    def _t():
        m = np.eye(4)
        m[:3, :3] = _rot(0, rng.uniform(-2, 2)) @ _rot(2, rng.uniform(-2, 2))
        m[:3, 3] = rng.uniform(-0.3, 0.3, 3)
        return m
    t_l0, t_f0 = _t(), _t()
    assert np.allclose(relay_map.relative_target(t_l0, t_l0, t_f0), t_f0)
    # 리더가 x 로 5cm 가면 목표도 팔로워 프레임에서 그만큼 이동한다
    t_l = t_l0.copy()
    t_l[:3, 3] += t_l0[:3, :3] @ np.array([0.05, 0, 0])
    moved = relay_map.relative_target(t_l, t_l0, t_f0)
    assert np.linalg.norm(moved[:3, 3] - t_f0[:3, 3]) == pytest.approx(0.05)


def test_cross_relay_engages_at_start_and_disengage_holds():
    """크로스 릴레이는 시작이 곧 첫 정합이고(실패하면 시작 자체를 접는다 —
    반쯤 열린 세션이 최악), 해제는 전송만 멈춘다: robotd 데드맨이 팔로워를
    세우는 쪽이 정직하다. 같은 모델(Piper끼리)은 기존 절대 복제 그대로 —
    행동이 하나도 안 바뀐다."""
    src = (REPO / "backend" / "app" / "services" / "relay.py").read_text()
    assert "self._engaged = not self._cross" in src, "같은 모델의 동작이 바뀌었다"
    start = src.split("def start", 1)[1].split("\n    def ", 1)[0]
    assert "_engage_locked()" in start, "시작이 첫 정합이 아니다"
    dis = src.split("def disengage", 1)[1].split("\n    def ", 1)[0]
    assert "publish" not in dis and "_engaged = False" in dis
    # 관절 크로스는 매핑 경로, 그리퍼는 절대 통과
    assert "_send_joint_mapped" in src
    mapped = src.split("def _send_joint_mapped", 1)[1].split("\n    def ", 1)[0]
    assert 'goal["gripper"] = float(values["gripper"])' in mapped
    # POSE 크로스는 상대 자세
    assert "relay_map.relative_target" in src


def test_an_external_leader_does_not_need_a_piper_master():
    """⚠ **실기: Piper 마스터가 없어 SO-101 을 리더로 쓰려는데 "마스터가
    없다"로 막혔다.** 같은쪽 리더 검사는 Piper 마스터 등록부를 보는데, 외부
    리더는 거기 없다 — 정체는 so101d 에게 묻는다: 연결·캘리브레이션을 확인하고,
    좌우는 **둘 다 지정됐을 때만** 강제한다 (팔 하나 구성에서 미지정을 막으면
    지정할 이유가 없는 사람까지 막는다)."""
    router = (REPO / "backend" / "app" / "routers" / "robots.py").read_text()
    body = router.split('"/relay/start"', 1)[1].split("@router.", 2)[1]
    body = router.split('"/relay/start"', 1)[1].split("\n@router", 1)[0]
    assert 'if body.leader_arm == "piper":' in body, "외부 리더 분기가 없다"
    assert "so101_client.info" in body
    assert "캘리브레이션" in body
    assert "lside and fside and lside != fside" in body, "좌우 강제가 무조건이다"


def test_pose_mode_refuses_wild_joint_jumps_instead_of_faulting():
    """⚠ **실기: POSE 모드가 관절을 엉뚱한 방향으로 밀어 로봇이 꼬여 죽었다.**
    5-DOF 리더 → 6-DOF 팔로워 자세 매핑은 pan·roll 같은 동작에서 도달
    불가이거나 IK 해가 한 관절을 수십 도 튕긴다(오프라인 실측: SO-101 pan
    15° → Piper 90°). 직교(mm/deg) 걸음 상한은 그걸 통과시키므로 **관절 공간**
    상한이 따로 있어야 한다 — 크게 뛰는 해는 보내지 않고 막는다."""
    src = (REPO / "backend" / "app" / "services" / "relay.py").read_text()
    assert "POSE_MAX_JOINT_STEP_DEG" in src
    body = src.split("def _send_pose", 1)[1].split("\n    def ", 1)[0]
    assert "POSE_MAX_JOINT_STEP_DEG" in body, "관절 걸음 상한이 _send_pose 에 없다"
    # IK 성공 뒤(3b) 여야 한다 — 실패 경로는 이미 막는다
    assert body.index("sol.ok") < body.index("POSE_MAX_JOINT_STEP_DEG")
    # 막을 때 발행하지 않는다: 상한 검사가 publish 보다 앞
    assert body.index("POSE_MAX_JOINT_STEP_DEG") < body.rindex("_writer.publish")


def test_a_failed_relay_start_leaves_no_session_open():
    """⚠ **실기: 시작이 반쯤 실패해 토크만 들어가고, 재시도하니 "수동 조작
    실행 중"이었다.** teleop_session 을 연 뒤로는 어느 단계에서 터지든 전부
    되감아야 한다 — 명령 세그먼트·리더·teleop 세션. 정리를 한 곳(_unwind_start)
    으로 모은다."""
    src = (REPO / "backend" / "app" / "services" / "relay.py").read_text()
    assert "_unwind_start" in src
    start = src.split("def start", 1)[1].split("\n    def ", 1)[0]
    # teleop_session.start 이후 전 구간이 하나의 try 로 감싸여 실패 시 되감는다
    assert start.count("_unwind_start(reader)") >= 2, "실패 경로가 되감지 않는다"
    unwind = src.split("def _unwind_start", 1)[1].split("\n    def ", 1)[0]
    assert "teleop_session.stop()" in unwind
    assert "self._writer = self._reader = None" in unwind


def test_cross_model_pose_is_disabled_on_both_ends():
    """⚠ **실기 결정: 말단(POSE) 모드는 5-DOF 리더에서 이상하다 — 비활성화.**
    프론트에서 선택지를 빼는 것만으로는 부족하다(API 로 켤 수 있다) —
    백엔드가 크로스 모델 POSE 를 거부해야 한다. Piper끼리의 POSE 는 별개라
    그대로 살아 있다. 되살리려면 양쪽을 함께 푼다."""
    relay = (REPO / "backend" / "app" / "services" / "relay.py").read_text()
    start = relay.split("def start", 1)[1].split("\n    def ", 1)[0]
    assert 'mode == "pose" and leader_arm != follower_arm' in start, \
        "백엔드가 크로스 POSE 를 안 막는다"
    panel = (REPO / "frontend" / "src" / "components" / "So101TeleopPanel.tsx").read_text()
    assert "'말단 POSE'" not in panel.split("{running && st &&", 1)[0], \
        "시작 화면에 POSE 선택지가 남아 있다"
    assert "const mode = 'joint' as const" in panel


# ── 수집 (5단계) — SO-101 리더로 Piper 를 녹화한다 ──


def test_the_so101_teleoperator_is_a_registered_lerobot_plugin_type():
    """녹화는 LeRobot record CLI 가 돌린다 — 텔레오퍼레이터가 플러그인으로
    등록돼 있어야 `--teleop.type=so101_leader_shm` 이 풀린다. 패키지 import 만으로
    등록되는 것이 계약이다 (register_third_party_plugins 가 그렇게 부른다)."""
    lerobot_robot_pipershm = pytest.importorskip("lerobot_robot_pipershm")
    from lerobot.teleoperators.config import TeleoperatorConfig

    assert "so101_leader_shm" in TeleoperatorConfig.get_known_choices()
    assert lerobot_robot_pipershm.So101ShmLeader.name == "so101_leader_shm"


def test_the_teleoperator_maps_deltas_from_the_connect_anchor(monkeypatch, tmp_path):
    """액션은 **팔로워(Piper) 관절계**다 — 학습·추론이 그대로 맞는다. connect
    순간이 정합이라 첫 액션 = 팔로워 현재 자세(점프 0), 이후 리더 변화량이
    관절쌍 부호로 얹힌다. 그리퍼는 절대 통과. 세그먼트는 읽기만 한다 —
    라이터는 로봇 클래스 하나여야 한다."""
    import json
    import time

    pytest.importorskip("lerobot_robot_pipershm")
    from lerobot_robot_pipershm import so101shmleader as M
    from lerobot_robot_pipershm.config_so101shmleader import So101ShmLeaderConfig

    # 캘리브레이션 — 폭 2048 (반바퀴)
    cal = {n: {"id": MOTOR_IDS[n], "drive_mode": 0, "homing_offset": 0,
               "range_min": 1024, "range_max": 3072} for n in SO101_JOINTS}
    (tmp_path / "so101_leader1.json").write_text(json.dumps(cal))
    monkeypatch.setenv("PIPER_SO101_CALIB_DIR", str(tmp_path))

    class _Reader:
        """가짜 StateReader — 이름별로 미리 정한 값을 돌려준다."""
        values = {
            "so101_leader1": {"joint1": 0.0, "joint2": 0.0, "joint3": 0.0,
                              "joint4": 0.0, "joint5": 0.0, "joint6": 0.0, "gripper": 40.0},
            "can0": {"joint1": 10.0, "joint2": 20.0, "joint3": -30.0,
                     "joint4": 5.0, "joint5": 0.0, "joint6": 0.0, "gripper": 0.0},
        }
        opened: list[str] = []

        def __init__(self, name):
            self.name = name
            _Reader.opened.append(name)

        def read(self):
            return {"values": dict(_Reader.values[self.name]),
                    "can_wall_ns": time.time_ns()}

        def age_s(self):
            return 0.0

        def close(self):
            pass

    monkeypatch.setattr(M, "StateReader", _Reader)
    t = M.So101ShmLeader(So101ShmLeaderConfig(port="so101_leader1", follower="can0"))
    t.connect()
    assert sorted(_Reader.opened) == ["can0", "so101_leader1"], "읽기 세그먼트 둘만 연다"
    assert "ActionWriter" not in open(M.__file__).read(), "리더가 명령 세그먼트를 만든다"

    first = t.get_action()
    # 정합 순간: 팔로워 현재 자세 그대로 (joint4 포함), 그리퍼는 리더 절대값
    for j, v in (("joint1", 10.0), ("joint2", 20.0), ("joint3", -30.0), ("joint4", 5.0)):
        assert first[f"{j}.pos"] == pytest.approx(v, abs=0.05), j
    assert first["gripper.pos"] == 40.0

    # 리더 pan 을 +50 (폭 2048 의 1/4 = 512틱 = 45°) → Piper joint1 도 +45°
    _Reader.values["so101_leader1"]["joint1"] = 50.0
    moved = t.get_action()
    from piper_robot.joints import JOINT_CALIBRATION
    lo, hi = JOINT_CALIBRATION["joint1"]
    per_deg_norm = 200.0 / ((hi - lo) / 1000.0)        # joint1 정규화 1당 각도
    assert moved["joint1.pos"] - first["joint1.pos"] == pytest.approx(45.0 * per_deg_norm, rel=0.02)
    assert moved["joint4.pos"] == pytest.approx(5.0, abs=0.05), "joint4 는 앵커 유지"
    assert set(t.action_features) == {f"{m}.pos" for m in
                                      ("joint1", "joint2", "joint3", "joint4",
                                       "joint5", "joint6", "gripper")}


def test_record_args_carry_the_so101_teleoperator_and_its_anchor_follower():
    """CLI 조립: teleop_type so101_leader → shm 타입으로 풀리고, 정합 앵커를
    읽을 팔로워(`--teleop.follower`)가 실린다. 프론트는 follower 를 따로 안
    보낸다 — 녹화 로봇과 같은 팔이라 recording.py 가 robot_port 로 채운다."""
    from app.core.config import settings
    from app.core.cli_mapping import build_record_args

    old = settings.robot_transport
    settings.robot_transport = "shm"
    try:
        args = build_record_args({"robot_type": "piper_follower", "robot_port": "can0",
                                  "teleop_type": "so101_leader",
                                  "teleop_port": "so101_leader1",
                                  "teleop_follower": "can0", "repo_id": "x/y"})
    finally:
        settings.robot_transport = old
    joined = " ".join(args)
    assert "--teleop.type=so101_leader_shm" in joined
    assert "--teleop.port=so101_leader1" in joined
    assert "--teleop.follower=can0" in joined
    assert "--robot.type=piper_follower_shm" in joined


def test_recording_start_asks_so101d_and_keeps_the_leader_out_of_prepare_arms():
    """외부 리더는 robotd 등록부에 없다 — `prepare_arms` 에 넘기면 "팔을 찾을 수
    없습니다"로 막힌다(릴레이가 처음에 그렇게 막혔던 것과 같은 병). 정체는
    so101d 에게 묻고(연결·캘리브레이션), prepare 에는 Piper 만 넘긴다.
    앵커 팔로워는 미리보기와 시작이 같은 조립기에서 채운다."""
    src = (REPO / "backend" / "app" / "routers" / "recording.py").read_text()
    assert 'params["teleop_follower"] = params.get("robot_port", "")' in src
    start = src.split("prepare_arms(arm_ports", 1)[0]
    assert "so101_client.info" in start
    assert "arm_ports = [body.robot_port]" in start, "리더가 prepare_arms 로 새어 간다"
    assert "캘리브레이션" in start


def test_the_recording_form_offers_so101_leaders_and_sends_the_type():
    """수집 폼은 so101d 가 연결·캘리브레이션한 팔만 Leader 로 얹고, 고르면
    teleop_type 을 so101_leader 로 보낸다 — 기본값(piper_leader)으로 가면
    LeRobot 이 SO-101 세그먼트를 Piper 리더로 읽어 관절이 어긋난다."""
    page = (REPO / "frontend" / "src" / "pages" / "RecordingPage.tsx").read_text()
    assert "sp.attached.calibrated" in page, "미캘리브레이션 팔이 선택지에 오른다"
    assert "teleop_type: 'so101_leader'" in page
    assert "(SO-101)" in page


def test_the_so101_side_control_looks_and_cycles_like_pipers():
    """좌/우 지정은 Piper 카드와 **같은 순환 버튼**이다 (왼팔 → 오른팔 → 미지정).
    두 카드의 같은 개념이 다르게 생기면(하나는 드롭박스) 사람이 "뭐가 다른가"를
    묻는다 — 실제로 물었다. 스타일 클래스까지 같아야 눈이 같은 것으로 읽는다."""
    page = (REPO / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    so101 = page.split("{/* SO-101 로봇 카드", 1)[1].split("{robotArms.map((arm) =>", 1)[0]
    assert "<select" not in so101, "SO-101 좌/우가 아직 드롭박스다"
    assert "att.side === 'left' ? 'right' : att.side === 'right' ? '' : 'left'" in so101, \
        "Piper 와 같은 순환(왼팔→오른팔→미지정)이 아니다"
    for shared in ("bg-purple-600/30 text-purple-300 border-purple-500/40",
                   "? '왼팔' :", "'오른팔' : '좌/우?'"):
        assert shared in so101, f"Piper 버튼과 다르게 생겼다: {shared}"


def test_can_and_serial_share_one_ui_frame():
    """⚠ **전송(CAN/시리얼)이 달라도 UI 틀은 하나다** — 사용자가 그렇게 정했다.
    포트 패널에는 **포트의 사실만**(CAN 카드와 같은 그리드·같은 카드 틀), [연결]
    하면 팔은 **로봇 패널**로 내려가 Piper 카드와 같은 틀에 선다. 팔의 일
    (좌/우·캘리브레이션·텔레옵·해제)이 포트 카드에 남아 있으면 안 된다."""
    page = (REPO / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    port_panel = page.split("{/* 포트 — 스캔된 CAN 포트 카드", 1)[1].split("{calibArm && (", 1)[0]
    # 시리얼 카드가 CAN 과 같은 그리드 안에 있다 (그리드가 하나뿐이다)
    assert port_panel.count("grid grid-cols-1 sm:grid-cols-2") == 1
    assert "serialPorts.map((sp) =>" in port_panel
    # 포트 카드에는 팔의 일이 없다
    for arm_thing in ("handleSerialSide", "setCalibArm", "setTeleopArm", "handleSerialRelease"):
        assert arm_thing not in port_panel, f"포트 카드에 팔의 일이 남아 있다: {arm_thing}"
    robot_panel = page.split("{/* 로봇 — 연결·등록된 팔 카드", 1)[1]
    assert "so101Arms.map((att) =>" in robot_panel, "SO-101 이 로봇 패널에 안 선다"
    assert "robotArms.length + so101Arms.length === 0" in robot_panel
    # Piper 로봇 카드와 같은 카드 틀·같은 버튼 크기
    so101 = robot_panel.split("so101Arms.map((att) =>", 1)[1].split("{robotArms.map((arm) =>", 1)[0]
    assert "rounded border p-2.5 transition-shadow" in so101
    assert so101.count("px-2.5 py-1 text-xs rounded") >= 3
