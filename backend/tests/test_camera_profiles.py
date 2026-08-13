"""카메라 프로파일 — 컨트롤 값 저장·안정 키·자동 모드 순서.

`feature/camera-profiles.md` 의 원인 7개 중 데몬 분리로 넷이 사라지고 남은 셋을
여기서 잠근다:

1. **(7) 자동 모드 순서** — `auto_exposure` 를 수동으로 돌리기 전에 `exposure` 를 쓰면
   커널이 조용히 무시한다. 에러가 안 나므로 테스트가 없으면 재현도 안 된다
2. **(2) 안정 키** — `/dev/videoN` 은 USB 재열거로 바뀐다. 키로 쓰면 설정이 사라진다
3. **(1) 저장** — 값이 실제로 프로파일에 들어가는가, 그리고 **자동 스위치는
   default 와 같아도 빠지지 않는가** (순서를 정하는 축이라 빠지면 1번이 깨진다)

그리고 두 데몬이 **같은 순서 로직 한 벌**을 쓰는지 (두 벌이면 한쪽만 고쳐진다).
"""

import ast
from pathlib import Path

import pytest

from piper_cam.controls import AUTO_SWITCHES, apply_controls, manual_value, plan

_REPO = Path(__file__).resolve().parents[2]


def _ctrl(name, **kw):
    d = {"name": name, "type": 1, "min": 0, "max": 10000, "step": 1,
         "default": 0, "value": 0, "inactive": False, "readonly": False}
    d.update(kw)
    return d


UVC = [
    # v4l2 auto_exposure 는 menu 이고 **1 = Manual, 3 = Aperture Priority(자동)** 이다
    _ctrl("auto_exposure", type=3, min=0, max=3, default=3, value=3),
    _ctrl("exposure_time_absolute", min=1, max=5000, default=166, value=166, inactive=True),
    _ctrl("white_balance_automatic", type=2, min=0, max=1, default=1, value=1),
    _ctrl("white_balance_temperature", min=2000, max=6500, default=4000, value=4000,
          inactive=True),
    _ctrl("brightness", min=-64, max=64, default=0, value=0),
    _ctrl("power_line_frequency", type=3, min=0, max=2, default=1, value=1, readonly=True),
]


# ── (7) 자동 모드 순서 ──

def test_switch_goes_manual_before_its_dependent():
    """스위치가 종속 값보다 **먼저** 써져야 한다. 이 순서가 전부다."""
    order = [n for n, _ in plan(UVC, {"auto_exposure": 1, "exposure_time_absolute": 312})]
    assert order.index("auto_exposure") < order.index("exposure_time_absolute")


def test_auto_exposure_manual_value_is_one_not_zero():
    """menu 라 bool 규칙(0=수동)이 안 통한다. 0 으로 쓰면 **정확히 거꾸로** 동작한다."""
    assert manual_value(UVC[0]) == 1
    assert manual_value(UVC[2]) == 0        # bool 스위치는 0


def test_switch_is_restored_to_what_the_profile_wants():
    """프로파일이 자동을 원하면 마지막에 자동으로 되돌아가야 한다."""
    writes = plan(UVC, {"auto_exposure": 3, "exposure_time_absolute": 312})
    assert writes[0] == ("auto_exposure", 1)     # 1단계: 종속 값을 쓰려고 수동으로
    assert writes[-1] == ("auto_exposure", 3)    # 4단계: 프로파일이 원한 자동으로


def test_switch_is_not_touched_when_no_dependent_is_written():
    """쓸 종속 값이 없으면 스위치를 흔들지 않는다 — 잘 돌던 자동노출을 건드릴 이유가 없다."""
    writes = plan(UVC, {"brightness": 10})
    assert [n for n, _ in writes] == ["brightness"]


def test_readonly_controls_never_get_planned():
    assert "power_line_frequency" not in {n for n, _ in plan(UVC, {"power_line_frequency": 2})}


def test_plan_is_pure():
    """순수 함수여야 장치 없이 테스트된다 — 입력을 건드리면 안 된다."""
    before = [dict(c) for c in UVC]
    plan(UVC, {"auto_exposure": 1, "exposure_time_absolute": 312})
    assert UVC == before


# ── read-back 분류 ──

def _fake_device(controls):
    """set 하면 값이 반영되고, 자동 스위치가 켜져 있으면 종속 값을 **조용히 무시**하는
    가짜 장치. 커널이 하는 짓을 그대로 흉내낸다."""
    state = {c["name"]: dict(c) for c in controls}

    def _sync():
        auto_on = state["auto_exposure"]["value"] != 1
        state["exposure_time_absolute"]["inactive"] = auto_on
        wb_auto = state["white_balance_automatic"]["value"] == 1
        state["white_balance_temperature"]["inactive"] = wb_auto

    def list_controls():
        _sync()
        return [dict(c) for c in state.values()]

    def set_control(name, value):
        c = state.get(name)
        if c is None or c["readonly"]:
            return False
        _sync()
        if c["inactive"]:      # ⚠ 커널은 에러를 안 낸다 — 그냥 버린다
            return True
        c["value"] = value
        return True

    return list_controls, set_control


def test_dict_order_alone_would_have_failed():
    """순서 없이 밀어 넣으면 노출이 안 먹는다는 것을 **직접 보인다.**

    이 테스트가 깨지면 가짜 장치가 커널을 안 닮은 것이고, 위 테스트들도 의미가 없다.
    """
    _, set_control = (lc, sc) = _fake_device(UVC)
    lc, sc = _fake_device(UVC)
    sc("exposure_time_absolute", 312)          # 자동이 켜진 채로 먼저 쓴다
    after = {c["name"]: c for c in lc()}
    assert after["exposure_time_absolute"]["value"] == 166   # 버려졌다


def test_apply_actually_lands_the_exposure():
    lc, sc = _fake_device(UVC)
    report = apply_controls(lc, sc, {"auto_exposure": 1, "exposure_time_absolute": 312,
                                     "brightness": 10})
    assert report["failed"] == 0
    after = {c["name"]: c for c in lc()}
    assert after["exposure_time_absolute"]["value"] == 312


def test_auto_wins_and_the_dependent_is_locked_not_failed():
    """자동을 원하면 종속 값은 무시된다. 그건 **정상**이라 실패로 세면 안 된다 —
    사용자가 고칠 수 없는 경고를 계속 보게 된다."""
    lc, sc = _fake_device(UVC)
    report = apply_controls(lc, sc, {"auto_exposure": 3, "exposure_time_absolute": 312})
    detail = {d["name"]: d for d in report["details"]}
    assert detail["exposure_time_absolute"]["status"] == "locked"
    assert report["failed"] == 0


def test_apply_never_raises_when_the_device_is_gone():
    """프로파일 적용 실패로 카메라 연결이 실패하면 본말전도다."""
    def boom():
        raise OSError("device gone")

    report = apply_controls(boom, lambda n, v: False, {"brightness": 10})
    assert report["skipped"] == 1


def test_budget_stops_the_writes():
    """컨트롤 하나가 몇 초씩 무는 장치가 있다(D405). 연결 전체를 붙잡으면 안 된다."""
    import time

    lc, _ = _fake_device(UVC)
    calls = []

    def slow(name, value):
        calls.append(name)
        time.sleep(0.05)
        return True

    report = apply_controls(lc, slow, {"brightness": 1, "contrast": 1, "saturation": 1,
                                       "hue": 1, "gamma": 1, "sharpness": 1},
                            budget_s=0.06)
    assert report["truncated"] or len(calls) < 6


# ── (2) 안정 키 ──

def test_profile_key_survives_device_renumbering():
    from app.services.camera_manager import CameraInfo

    before = CameraInfo(id="/dev/video2", name="HD Webcam", usb_port="4-3:1.0")
    after = CameraInfo(id="/dev/video6", name="HD Webcam", usb_port="4-3:1.0")
    assert before.profile_key == after.profile_key == "usb:4-3:1.0"


def test_realsense_reuses_its_id_instead_of_inventing_a_second_key():
    """cam_id 가 이미 `rs:<시리얼>:<스트림>` 이라 안정적이다 — 키를 또 만들면
    같은 사실이 두 벌이 된다."""
    from app.services.camera_manager import CameraInfo

    cam = CameraInfo(id="rs:250122070363:color", name="D405 Color",
                     cam_type="realsense", serial="250122070363")
    assert cam.profile_key == cam.id


def test_session_restore_matches_by_key_not_dev_path():
    from app.services.camera_manager import CameraInfo, CameraManager

    mgr = CameraManager()
    mgr.cameras["/dev/video6"] = CameraInfo(id="/dev/video6", name="HD Webcam",
                                            usb_port="4-3:1.0")
    got = mgr.match_saved({"id": "/dev/video2", "key": "usb:4-3:1.0", "name": "HD Webcam"})
    assert got is not None and got.id == "/dev/video6"


def test_name_fallback_refuses_when_two_cameras_share_a_name():
    """같은 모델 두 대를 이름으로 맞추면 설정이 엉뚱한 카메라에 붙는다.
    틀리게 복원하느니 안 하는 게 낫다."""
    from app.services.camera_manager import CameraInfo, CameraManager

    mgr = CameraManager()
    for dev, port in (("/dev/video0", "1-1:1.0"), ("/dev/video4", "1-2:1.0")):
        mgr.cameras[dev] = CameraInfo(id=dev, name="HD Webcam", usb_port=port)
    assert mgr.match_saved({"id": "/dev/video2", "key": "usb:9-9:9.9",
                            "name": "HD Webcam"}) is None


def test_session_records_the_key():
    """저장에 키가 안 들어가면 복원이 `/dev/videoN` 으로 되돌아간다."""
    src = (_REPO / "backend" / "app" / "services" / "camera_manager.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "save_session")
    keys = {k.value for n in ast.walk(fn) if isinstance(n, ast.Dict)
            for k in n.keys if isinstance(k, ast.Constant)}
    assert "key" in keys


# ── (1) 저장 ──

class _FakeCam:
    profile_key = "usb:4-3:1.0"
    cam_type = "opencv"
    usb_port = "4-3:1.0"
    name = "HD Webcam"
    serial = ""
    stream_type = ""
    id = "/dev/video2"
    label = ""
    width = 640
    height = 480
    fps = 30
    fourcc = "MJPG"

    def __init__(self, controls):
        self._controls = controls

    def get_controls(self):
        return self._controls


def test_capture_keeps_auto_switches_even_at_default():
    """자동 스위치는 적용 순서를 정하는 축이다. default 라고 빼면 종속 값이
    조용히 무시되는 그 상태로 돌아간다."""
    from app.services import camera_profiles

    values = camera_profiles.capture([_FakeCam(UVC)])
    saved = values["cameras"][0]["controls"]
    assert "auto_exposure" in saved           # value == default 인데도 남는다
    assert "white_balance_automatic" in saved
    assert "brightness" not in saved          # 평범한 항목은 default 면 뺀다


def test_capture_stores_values_only_not_ranges():
    """min/max/default 는 장치가 진실이다. 저장하면 옛 범위로 클램프하게 된다."""
    from app.services import camera_profiles

    entry = camera_profiles.capture([_FakeCam(UVC)])["cameras"][0]
    assert set(entry) == {"key", "match", "stream", "controls"}
    assert all(isinstance(v, (int, float)) for v in entry["controls"].values())


def test_float_options_survive_capture():
    """RealSense `depth_units` 는 1e-4 다. `int()` 를 씌우면 0 이 되고,
    그 0 을 다시 밀어 넣으면 깊이 스케일이 0 이 된다 — 실기에서 실제로 걸렸다."""
    from app.services import camera_profiles

    cam = _FakeCam([_ctrl("some_float_option", default=0.001, value=0.0001)])
    saved = camera_profiles.capture([cam])["cameras"][0]["controls"]
    assert saved["some_float_option"] == pytest.approx(0.0001)


def test_depth_units_is_never_captured():
    """깊이 스케일은 픽셀값의 **뜻**을 정한다 — 데이터셋 계약이지 조명 설정이 아니다."""
    from app.services import camera_profiles

    cam = _FakeCam([_ctrl("depth_units", default=0.001, value=0.0001)])
    assert camera_profiles.capture([cam])["cameras"][0]["controls"] == {}


def test_capture_skips_readonly():
    from app.services import camera_profiles

    saved = camera_profiles.capture([_FakeCam(UVC)])["cameras"][0]["controls"]
    assert "power_line_frequency" not in saved


def test_controls_for_refuses_to_guess_by_name():
    """카메라 한 대만 볼 때는 "후보가 하나" 규칙을 쓸 수 없다.
    이름으로 넘겨짚어 남의 노출값을 밀어 넣느니 적용 안 하는 게 낫다."""
    from app.services import camera_profiles

    entries = [{"key": "usb:9-9:9.9",
                "match": {"name": "HD Webcam", "last_dev": "/dev/video9"},
                "controls": {"brightness": 42}}]
    camera_profiles.active_entries = lambda: entries
    try:
        assert camera_profiles.controls_for(_FakeCam([])) == {}
    finally:
        del camera_profiles.active_entries


# ── 한 벌만 존재하는가 ──

def test_both_daemons_share_one_ordering_implementation():
    """순서 규칙이 두 벌이면 한쪽만 고쳐진다 — 자동 모드 함정은 v4l2 든
    RealSense 든 같은 함정이다."""
    rs_src = (_REPO / "rs" / "piper_rs" / "hub.py").read_text()
    tree = ast.parse(rs_src)
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert "piper_cam" in mods or any(m.startswith("piper_cam") for m in mods)


@pytest.mark.parametrize("path", [
    _REPO / "cam" / "piper_cam" / "hub.py",
    _REPO / "rs" / "piper_rs" / "hub.py",
])
def test_hubs_expose_apply_controls(path):
    """게이트웨이가 `cam_type` 으로 허브만 고르면 되게 **같은 이름**이어야 한다."""
    tree = ast.parse(path.read_text())
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert {"apply_controls", "last_apply_report"} <= names


@pytest.mark.parametrize("daemon", ["camerad", "rsd"])
def test_daemons_allow_the_new_methods(daemon):
    """허용 목록에 없으면 RPC 가 조용히 "알 수 없는 메서드" 로 떨어진다."""
    tree = ast.parse((_REPO / "daemons" / f"{daemon}.py").read_text())
    allowed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_METHODS" for t in node.targets
        ):
            allowed = {e.value for e in ast.walk(node.value)
                       if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    assert {"apply_controls", "last_apply_report"} <= allowed


def test_every_auto_switch_has_a_dependent():
    """종속 값이 없는 스위치는 표에 있을 이유가 없다 — 있으면 오타다."""
    assert all(deps for deps in AUTO_SWITCHES.values())
