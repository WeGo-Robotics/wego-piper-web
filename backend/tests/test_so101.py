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
    assert "serialPorts.length > 0 &&" in page, "데몬 없을 때 패널이 안 접힌다"
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
