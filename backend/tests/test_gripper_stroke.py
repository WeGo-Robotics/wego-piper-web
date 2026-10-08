"""그리퍼 행정(70/100mm) — 팔마다 고른다 (2026-10-08).

Piper 그리퍼는 소형 70mm / 대형 100mm 두 가지고, 리더·팔로워에 섞여 달린다. 정규화(0..100)는
행정에 대한 비율이라 팔마다 raw 상한이 달라야 "100" 이 그 팔의 끝이다.

실기(2026-10-08): 대형 그리퍼가 소형 상한(68000)으로 정규화돼, 시스템이 팔로워에게 68mm 까지만
명령하고 토크로 버텼다 — 손으로도 끝까지 안 벌려졌다. 같은 순간 리더(대형)는 raw 104400 까지
열려 있었다.
"""

from pathlib import Path

import pytest

pytest.importorskip("piper_robot")

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(tmp_path, monkeypatch):
    from piper_robot import gripper_store as G

    monkeypatch.setattr(G, "PATH", tmp_path / "gripper.json")
    monkeypatch.setattr(G, "_cache", None)
    monkeypatch.setattr(G, "_strokes", {})
    monkeypatch.setattr(G, "_ends", {})
    return G


def test_an_arm_without_a_setting_keeps_the_old_range(store):
    """이 기능을 넣었다고 쓰던 팔의 변환이 바뀌면 안 된다 — 기본은 소형, 예전 68000 그대로."""
    from piper_robot.joints import JOINT_CALIBRATION, GRIPPER_RAW_MAX, DEFAULT_GRIPPER_STROKE_MM

    assert store.stroke_mm("can0") == DEFAULT_GRIPPER_STROKE_MM == 70
    assert store.raw_max_um("can0") == JOINT_CALIBRATION["gripper"][1] == 68000
    assert GRIPPER_RAW_MAX[DEFAULT_GRIPPER_STROKE_MM] == JOINT_CALIBRATION["gripper"][1], \
        "소형 상한이 vendor 와 대조되는 정본에서 갈렸다"


def test_the_stroke_is_per_arm_and_survives_a_restart(store):
    assert store.set_stroke("can0", 100) == 100
    assert store.stroke_mm("can0") == 100 and store.stroke_mm("can1") == 70, "다른 팔까지 바뀌었다"
    store._cache = None                                   # robotd 재시작
    assert store.stroke_mm("can0") == 100 and store.stroke_mm("can1") == 70


def test_saving_the_force_does_not_erase_the_stroke_and_vice_versa(store):
    """⚠ 힘과 행정은 한 파일이다 — 쓰는 곳이 둘이면 한쪽을 저장하는 순간 다른 쪽이 지워진다."""
    store.set_stroke("can0", 100)
    store.set_effort("can0", 2.5)
    store._cache = None
    assert store.stroke_mm("can0") == 100 and store.effort_nm("can0") == 2.5
    store.set_stroke("can1", 100)
    store._cache = None
    assert store.effort_nm("can0") == 2.5 and store.stroke_mm("can1") == 100


@pytest.mark.parametrize("bad", [50, 71, 0, -70, 1000, "abc", None])
def test_a_stroke_that_is_not_a_gripper_is_refused_not_clamped(store, bad):
    """70 과 100 사이의 값은 존재하지 않는 그리퍼다 — 가까운 쪽으로 맞추면 틀린 상한으로 달린다."""
    with pytest.raises(ValueError):
        store.set_stroke("can0", bad)
    assert store.stroke_mm("can0") == 70, "거절했는데 값이 바뀌었다"


def test_a_hand_edited_file_cannot_send_an_arm_to_an_unknown_range(store):
    store.PATH.write_text('{"arms": {}, "strokes": {"can0": 85, "can1": 100}}')
    assert store.stroke_mm("can0") == 70 and store.stroke_mm("can1") == 100


def test_an_old_file_without_strokes_still_loads(store):
    """이 기능 전에 저장된 gripper.json 은 `strokes` 가 없다."""
    store.PATH.write_text('{"arms": {"can0": 2.0}}')
    assert store.effort_nm("can0") == 2.0 and store.stroke_mm("can0") == 70


def test_the_same_normalized_value_means_a_wider_opening_on_the_large_gripper():
    from piper_robot.joints import GRIPPER_RAW_MAX, denormalize_joint, normalize_joint

    small, large = GRIPPER_RAW_MAX[70], GRIPPER_RAW_MAX[100]
    assert denormalize_joint("gripper", 100, small) == small
    assert denormalize_joint("gripper", 100, large) == large, "대형의 100 이 끝이 아니다"
    assert denormalize_joint("gripper", 50, large) == large // 2
    assert normalize_joint("gripper", large, large) == 100.0
    # 범위를 안 주면 예전 동작 — 소형 기본
    assert denormalize_joint("gripper", 100) == small
    assert normalize_joint("gripper", small) == 100.0


def test_the_large_cap_stays_inside_the_measured_end_stop():
    """⚠ 실기(2026-10-08): 대형 팔로워가 raw 99500 까지 열렸다. 상한이 그 너머면 "100" 이 스톱을
    힘 설정으로 누르는 명령이 된다 — 소형이 공칭 70 에 68000 으로 2mm 를 남기는 것과 같은 여유를
    지킨다. 더 열려면 **다시 재서** 이 숫자와 같이 고친다."""
    from piper_robot.joints import GRIPPER_RAW_MAX

    assert GRIPPER_RAW_MAX[100] <= 99500 - 1500, "대형 상한이 실측 끝에 너무 가깝거나 너머다"
    assert GRIPPER_RAW_MAX[100] > GRIPPER_RAW_MAX[70], "대형이 소형보다 안 열린다"


def test_the_range_only_changes_the_gripper_never_a_joint():
    """⚠ `gripper_raw_max` 가 관절에 새면 팔이 엉뚱한 곳으로 간다 — 인자는 그리퍼 전용이다."""
    from piper_robot.joints import denormalize_all, normalize_all

    norm = {"joint1": 30.0, "joint5": -20.0, "gripper": 40.0}
    base = denormalize_all(norm)
    big = denormalize_all(norm, 100000)  # 임의의 다른 상한 — 표 값과 무관한 검사
    assert {k: v for k, v in base.items() if k != "gripper"} == \
           {k: v for k, v in big.items() if k != "gripper"}
    assert big["gripper"] == 40000 and base["gripper"] == int(0.4 * 68000)
    raw = {"joint1": 45000, "gripper": 50000}
    assert normalize_all(raw, 100000)["gripper"] == 50.0
    assert normalize_all(raw, 100000)["joint1"] == normalize_all(raw)["joint1"]


def test_every_gripper_conversion_goes_through_the_arms_range():
    """⚠ 한 경로만 고치면 그 경로에서만 "100" 이 끝이다 — 읽기(상태)·파킹·명령 셋 다 탄다.
    읽기만 빠지면 화면의 그리퍼 값이 팔의 실제 폭과 어긋난다."""
    arm = (REPO / "robot" / "piper_robot" / "arm.py").read_text()
    pub = (REPO / "robot" / "piper_robot" / "publish.py").read_text()
    assert "normalize_all(raw, gripper_store.raw_max_um(self.iface))" in arm, "읽기가 안 탄다"
    assert "denormalize_all(target, gripper_store.raw_max_um(self.iface))" in arm, "파킹이 안 탄다"
    assert "denormalize_all(values, gripper_store.raw_max_um(self.iface))" in pub, "명령이 안 탄다"


def test_robotd_exposes_both_verbs_and_only_for_arms_it_drives():
    src = (REPO / "daemons" / "robotd.py").read_text()
    methods = src.split("_METHODS = {", 1)[1].split("}", 1)[0]
    assert '"get_gripper_stroke"' in methods and '"set_gripper_stroke"' in methods
    for verb in ("get_gripper_stroke", "set_gripper_stroke"):
        body = src.split(f"def {verb}", 1)[1].split("\n    def ", 1)[0]
        assert "if iface not in self.arms" in body, f"{verb}: 모르는 팔도 지원한다고 답한다"


@pytest.fixture
def gw(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import robot_manager as R

    sent = []

    def fake(m, *a, **k):
        sent.append((m, a))
        if m == "get_gripper_stroke":
            return {"iface": a[0], "stroke_mm": 70, "options_mm": [70, 100],
                    "default_mm": 70, "supported": True}
        if m == "set_gripper_stroke":
            return {"iface": a[0], "stroke_mm": a[1], "options_mm": [70, 100],
                    "default_mm": 70, "supported": True}
        if m in ("capture_gripper_end", "clear_gripper_end"):
            return {"iface": a[0], "stroke_mm": 100, "options_mm": [70, 100], "default_mm": 70,
                    "end_um": 82860 if m == "capture_gripper_end" else None,
                    "raw_max_um": 82860 if m == "capture_gripper_end" else 98000,
                    "table_raw_max_um": 98000, "supported": True}
        return None

    monkeypatch.setattr(R, "_call", fake)
    with TestClient(app) as c:
        yield c, sent


@pytest.mark.parametrize("busy", ["INFERENCE", "RECORDING", "TELEOP"])
def test_a_moving_activity_refuses_the_change(gw, monkeypatch, busy):
    """⚠ 바꾸는 순간 같은 정규화 값이 다른 물리 폭이 된다 — 추론·텔레옵이면 그리퍼가 튀고,
    녹화면 한 데이터셋 안에서 값의 뜻이 갈린다."""
    from app.services import exclusivity as X

    c, sent = gw
    monkeypatch.setattr(X, "is_running", lambda a: a == getattr(X.Activity, busy))
    r = c.post("/api/robots/gripper-stroke", json={"iface": "can0", "stroke_mm": 100})
    assert r.status_code == 409, r.text
    assert not [m for m, _ in sent if m == "set_gripper_stroke"], "거절했는데 robotd 에 보냈다"


def test_idle_takes_the_change_and_hands_robotd_the_arm_and_the_stroke(gw, monkeypatch):
    from app.services import exclusivity as X

    c, sent = gw
    monkeypatch.setattr(X, "is_running", lambda a: False)
    r = c.post("/api/robots/gripper-stroke", json={"iface": "can0", "stroke_mm": 100})
    assert r.status_code == 200 and r.json()["stroke_mm"] == 100
    assert ("set_gripper_stroke", ("can0", 100)) in sent


def test_a_stroke_that_is_not_offered_is_a_400_before_robotd_hears_of_it(gw, monkeypatch):
    from app.services import exclusivity as X

    c, sent = gw
    monkeypatch.setattr(X, "is_running", lambda a: False)
    assert c.post("/api/robots/gripper-stroke", json={"iface": "can0", "stroke_mm": 85}).status_code == 400
    assert not [m for m, _ in sent if m == "set_gripper_stroke"]


def test_an_arm_robotd_does_not_drive_has_no_selector(monkeypatch):
    """시뮬·SO-101 처럼 robotd 가 모르는 팔은 이 행정을 쓰는 변환 경로가 없다 — 404 가
    "아예 안 그린다" 의 신호다 (그리퍼 힘과 같은 규칙)."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import exclusivity as X
    from app.services import robot_manager as R

    monkeypatch.setattr(R, "_call", lambda m, *a, **k: {"iface": a[0], "supported": False})
    monkeypatch.setattr(X, "is_running", lambda a: False)
    with TestClient(app) as c:
        assert c.get("/api/robots/gripper-stroke", params={"iface": "sim_follower1"}).status_code == 404
        assert c.post("/api/robots/gripper-stroke",
                      json={"iface": "sim_follower1", "stroke_mm": 100}).status_code == 404
    comp = (REPO / "frontend" / "src" / "components" / "GripperStrokeSelect.tsx").read_text()
    assert "if (!iface || unsupported) return null" in comp


def test_the_selector_sits_outside_the_role_branch():
    """리더·팔로워 **둘 다** 그리퍼가 달린다 — 역할 분기 안에 두면 한쪽 팔은 못 고른다."""
    src = (REPO / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    head = src.split("<GripperStrokeSelect", 1)
    assert len(head) == 2, "선택 UI 가 화면에 없다"
    after = head[1].split("arm.role === 'follower'", 1)[0]
    assert "{/*" not in after.split("/>", 1)[0], "선택 UI 가 역할 분기 안으로 들어갔다"
    assert src.index("<GripperStrokeSelect") < src.index("arm.role === 'follower' || arm.role === 'unknown'")


# ── 열림 끝 맞추기 — 리더 핸들은 그리퍼 모델이 아니다 ──
#
# 실기(2026-10-08): 리더를 끝까지 열어도 지령(0x159)이 최대 82860µm 이었고 팔로워(대형)는 99500µm
# 까지 열릴 수 있었다. 비율로 매핑하니 팔로워가 82% 에서 멈췄다 — 리더의 끝을 재서 저장한다.


def test_a_measured_end_beats_the_table_and_survives_a_restart(store):
    assert store.raw_max_um("can1") == 68000 and store.end_um("can1") is None
    assert store.set_end("can1", 82860.4) == 82860
    assert store.raw_max_um("can1") == 82860 and store.raw_max_um("can0") == 68000, "다른 팔까지 바뀌었다"
    store._cache = None                                   # robotd 재시작
    assert store.end_um("can1") == 82860 and store.raw_max_um("can1") == 82860


def test_the_leader_full_open_becomes_one_hundred_once_its_end_is_saved(store):
    """이 기능의 이유: 저장 전엔 리더 끝(82860)이 100 이 못 되고, 저장하면 100 이다."""
    from piper_robot.joints import normalize_joint

    store.set_stroke("can1", 100)
    before = normalize_joint("gripper", 82860, store.raw_max_um("can1"))
    assert 80 < before < 90, f"저장 전 리더 끝이 {before}"       # 98000 기준 → 84.5
    store.set_end("can1", 82860)
    assert normalize_joint("gripper", 82860, store.raw_max_um("can1")) == 100.0


@pytest.mark.parametrize("bad", [0, 5000, 19999, 120001, 1e9, -1, "abc", None])
def test_an_end_outside_the_range_is_refused_not_clamped(store, bad):
    """⚠ 덜 열린 채 눌렀을 때 가까운 값으로 맞춰 저장하면 그 팔의 "100" 이 조용히 틀린 자리가 된다 —
    상한이 작으면 작은 움직임이 100 이 되어 팔로워가 튄다."""
    with pytest.raises(ValueError):
        store.set_end("can1", bad)
    assert store.end_um("can1") is None, "거절했는데 저장됐다"


def test_changing_the_stroke_drops_the_measured_end(store):
    """다른 그리퍼에서 잰 끝이 새 그리퍼에 남으면 안 된다."""
    store.set_end("can1", 82860)
    store.set_stroke("can1", 70)
    assert store.end_um("can1") is None and store.raw_max_um("can1") == 68000
    store._cache = None
    assert store.end_um("can1") is None, "지웠는데 다시 읽혔다"


def test_clearing_returns_to_the_table_and_other_arms_keep_theirs(store):
    store.set_end("can0", 90000)
    store.set_end("can1", 82860)
    store.clear_end("can1")
    assert store.end_um("can1") is None and store.end_um("can0") == 90000
    store.clear_end("can9")                                # 없는 팔을 지워도 조용하다
    store._cache = None
    assert store.end_um("can0") == 90000 and store.end_um("can1") is None


def test_the_end_is_in_the_same_file_and_no_writer_erases_the_others(store):
    """⚠ 힘·행정·끝은 한 파일이다 — 어느 하나를 저장해도 나머지가 살아야 한다."""
    store.set_end("can1", 82860)
    store.set_effort("can1", 2.0)
    store.set_stroke("can0", 100)
    store._cache = None
    assert store.end_um("can1") == 82860 and store.effort_nm("can1") == 2.0
    assert store.stroke_mm("can0") == 100


def test_a_hand_edited_end_outside_the_range_is_dropped(store):
    store.PATH.write_text('{"ends": {"can0": 5, "can1": 82860, "can2": "x"}}')
    assert store.end_um("can0") is None and store.end_um("can2") is None
    assert store.end_um("can1") == 82860


def test_the_dict_the_screen_reads_names_the_end_and_the_table_it_would_fall_back_to(store):
    d = store.stroke_dict("can1")
    assert d["end_um"] is None and d["raw_max_um"] == d["table_raw_max_um"] == 68000
    store.set_end("can1", 82860)
    d = store.stroke_dict("can1")
    assert d["end_um"] == 82860 and d["raw_max_um"] == 82860 and d["table_raw_max_um"] == 68000


def test_the_capture_reads_the_same_source_as_the_normalized_state():
    """⚠ 화면이 보는 값과 저장되는 값이 다른 신호면 "끝까지 열었는데 100 이 안 된다" 가 된다.
    상태 읽기와 그리퍼 raw 읽기는 `_raw_state_locked` 한 곳을 탄다."""
    arm = (REPO / "robot" / "piper_robot" / "arm.py").read_text()
    assert arm.count("self._raw_state_locked()") == 2, "읽기 경로가 둘로 갈렸다"
    assert "def read_gripper_raw" in arm
    norm = arm.split("def read_joints_normalized", 1)[1].split("\n    def ", 1)[0]
    assert "normalize_all(raw, gripper_store.raw_max_um(self.iface))" in norm


def test_robotd_exposes_the_end_verbs_for_driven_arms_only():
    src = (REPO / "daemons" / "robotd.py").read_text()
    methods = src.split("_METHODS = {", 1)[1].split("}", 1)[0]
    assert '"capture_gripper_end"' in methods and '"clear_gripper_end"' in methods
    for verb in ("capture_gripper_end", "clear_gripper_end"):
        body = src.split(f"def {verb}", 1)[1].split("\n    def ", 1)[0]
        assert "not in self.arms" in body or "self.arms.get(iface)" in body, f"{verb}: 모르는 팔도 지원한다고 답한다"
    cap = src.split("def capture_gripper_end", 1)[1].split("\n    def ", 1)[0]
    assert "arm.read_gripper_raw()" in cap, "열림을 상태 읽기와 다른 곳에서 읽는다"


@pytest.fixture
def gw_arms(gw, monkeypatch):
    """게이트웨이 등록부에 리더(can1)·팔로워(can0)를 둔다."""
    from app.services import robot_manager as R

    arms = {"can0": R.ArmInfo(iface="can0", role="follower"),
            "can1": R.ArmInfo(iface="can1", role="leader")}
    monkeypatch.setattr(R.robot_manager, "arms", arms)
    return gw


def test_only_a_leader_can_have_its_end_measured(gw_arms, monkeypatch):
    """⚠ 팔로워의 "100" 이 측정한 물리 끝이면 완전히 열 때마다 스톱을 힘 설정으로 누른다."""
    from app.services import exclusivity as X

    c, sent = gw_arms
    monkeypatch.setattr(X, "is_running", lambda a: False)
    r = c.post("/api/robots/gripper-stroke/end", json={"iface": "can0"})
    assert r.status_code == 400 and "리더" in r.json()["detail"]
    assert not [m for m, _ in sent if m == "capture_gripper_end"], "거절했는데 robotd 에 보냈다"
    r = c.post("/api/robots/gripper-stroke/end", json={"iface": "can9"})
    assert r.status_code == 400


def test_a_leader_capture_goes_to_robotd(gw_arms, monkeypatch):
    from app.services import exclusivity as X

    c, sent = gw_arms
    monkeypatch.setattr(X, "is_running", lambda a: False)
    r = c.post("/api/robots/gripper-stroke/end", json={"iface": "can1"})
    assert r.status_code == 200, r.text
    assert ("capture_gripper_end", ("can1",)) in sent


def test_clearing_is_allowed_for_any_arm_so_the_way_back_is_never_blocked(gw_arms, monkeypatch):
    from app.services import exclusivity as X

    c, sent = gw_arms
    monkeypatch.setattr(X, "is_running", lambda a: False)
    for iface in ("can0", "can1"):
        assert c.post("/api/robots/gripper-stroke/end",
                      json={"iface": iface, "clear": True}).status_code == 200
    assert [m for m, _ in sent].count("clear_gripper_end") == 2


@pytest.mark.parametrize("busy", ["INFERENCE", "RECORDING", "TELEOP"])
def test_the_end_cannot_move_while_the_arm_moves(gw_arms, monkeypatch, busy):
    from app.services import exclusivity as X

    c, sent = gw_arms
    monkeypatch.setattr(X, "is_running", lambda a: a == getattr(X.Activity, busy))
    for body in ({"iface": "can1"}, {"iface": "can1", "clear": True}):
        assert c.post("/api/robots/gripper-stroke/end", json=body).status_code == 409
    assert not [m for m, _ in sent if m.endswith("_gripper_end")], "거절했는데 robotd 에 보냈다"


def test_a_refusal_from_robotd_reaches_the_screen_as_a_400(monkeypatch):
    """덜 열린 채 눌렀다면 robotd 가 사유를 말하고, 화면은 그걸 그대로 보여 준다."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import exclusivity as X
    from app.services import robot_manager as R

    monkeypatch.setattr(R.robot_manager, "arms", {"can1": R.ArmInfo(iface="can1", role="leader")})
    monkeypatch.setattr(X, "is_running", lambda a: False)
    monkeypatch.setattr(R, "_call", lambda m, *a, **k: {
        "iface": a[0], "supported": True, "error": "지금 열림이 3.0mm 입니다 — 끝까지 연 상태에서 눌러 주세요"})
    with TestClient(app) as c:
        r = c.post("/api/robots/gripper-stroke/end", json={"iface": "can1"})
    assert r.status_code == 400 and "끝까지" in r.json()["detail"]


def test_the_screen_shows_the_end_only_on_the_leader():
    comp = (REPO / "frontend" / "src" / "components" / "GripperStrokeSelect.tsx").read_text()
    assert "role === 'leader'" in comp and "/robots/gripper-stroke/end" in comp
    page = (REPO / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    assert "<GripperStrokeSelect iface={arm.iface} role={arm.role} />" in page
