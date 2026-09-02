"""프로파일 재현성 계약 — "반드시 있어야 하는 것" 검토의 구현
(feature/camera-profiles.md 검토 절 G1~G3).

프로파일의 존재 이유는 재현이다. capture 는 장치 상태를 충실히 기록할 뿐
그 상태가 재현 가능한지 모른다 — 그래서 종속 값은 default 라도 저장하고(G2),
validate 가 자동 상태·값 누락을 경고하고(G1), 작업 적용이 안 덮는 카메라를
말한다(G3).
"""

from types import SimpleNamespace

from app.services import camera_profiles


def _cam(controls, **kw):
    return SimpleNamespace(
        profile_key="k1", cam_type="opencv", usb_port="1-1", name="웹캠",
        serial="", stream_type="color", id="/dev/video0",
        width=640, height=480, fps=30, fourcc="MJPG", label="탑뷰",
        get_controls=lambda: controls, **kw)


def _ctrl(name, value, default=None, **kw):
    return {"name": name, "value": value,
            "default": value if default is None else default, **kw}


def test_capture_keeps_manual_exposure_even_at_default():
    """⚠ G2 — 종속 값을 default 라고 빼면 프로파일 **전환**이 완결되지 않는다:
    야간(노출 2000) 적용 뒤 주간(노출=default 라 미저장)을 적용하면 스위치만
    수동으로 돌아오고 2000 이 잔류한다. 그래서 노출·WB·gain 은 항상 저장한다."""
    out = camera_profiles.capture([_cam([
        _ctrl("auto_exposure", 1),                       # 수동 — 스위치는 원래 저장
        _ctrl("exposure_time_absolute", 333, default=333),   # default 와 같다
        _ctrl("brightness", 0, default=0),                   # 독립 값 — 여전히 뺀다
    ])])
    controls = out["cameras"][0]["controls"]
    assert controls.get("exposure_time_absolute") == 333, "default 노출이 빠졌다 — 전환 잔류 구멍"
    assert "brightness" not in controls, "독립 값까지 저장하면 파일 축소 규칙이 사라진다"


def test_validate_flags_a_profile_captured_on_auto():
    """G1 — AE 켠 채 캡처한 프로파일은 '자동 모드'를 충실히 재현한다.
    조명이 바뀌면 카메라가 따라가고, 그게 이 기능이 막으려던 바로 그 일이다."""
    warns = camera_profiles.validate([{
        "key": "k1", "match": {"stream_type": "color", "last_dev": "x"},
        "controls": {"auto_exposure": 3,                       # 3 = 자동 (menu)
                     "white_balance_automatic": 0, "white_balance_temperature": 4600},
    }])
    texts = [w["text"] for w in warns]
    assert any("노출" in t and "자동" in t for t in texts), texts
    assert not any("화이트밸런스" in t for t in texts), "수동+값 있는 WB 에 경고를 냈다"


def test_validate_flags_manual_without_a_value():
    warns = camera_profiles.validate([{
        "key": "k1", "match": {"stream_type": "color", "last_dev": "x"},
        "controls": {"enable_auto_exposure": 0,                # 수동인데
                     "enable_auto_white_balance": 0, "white_balance": 4600},
        # exposure/gain 이 없다
    }])
    texts = [w["text"] for w in warns]
    assert any("노출" in t and "값이 없" in t for t in texts), texts


def test_validate_skips_depth_and_flags_missing_match():
    warns = camera_profiles.validate([
        {"key": "", "match": {"stream_type": "depth"}, "controls": {}},
    ])
    texts = [w["text"] for w in warns]
    assert any("match" in t for t in texts), "match 부재를 안 잡았다"
    assert not any("노출" in t for t in texts), "깊이 스트림에 노출 경고를 냈다"


def test_a_clean_manual_profile_has_no_warnings():
    """경고가 남발되면 아무도 안 읽는다 — 제대로 캡처된 프로파일은 조용해야 한다."""
    warns = camera_profiles.validate([{
        "key": "k1", "match": {"stream_type": "color", "last_dev": "x"},
        "controls": {
            "auto_exposure": 1, "exposure_time_absolute": 333, "gain": 10,
            "white_balance_automatic": 0, "white_balance_temperature": 4600,
        },
    }])
    assert warns == [], warns


def test_apply_for_task_names_the_uncovered_cameras(monkeypatch):
    """G3 — 작업이 쓰는 카메라를 프로파일이 안 덮으면 그 카메라는 기준 없이
    돈다. 막을 일은 아니지만(일부만 다루는 프로파일도 정당하다) 시작 전에
    이름을 대고 말해야 한다. 문구는 백엔드가 만든다 — device_watch 와 같은 규칙."""
    cams = {
        "c1": SimpleNamespace(id="c1", label="탑뷰", name="a", connected=True),
        "c2": SimpleNamespace(id="c2", label="손목", name="b", connected=True),
    }
    from app.services import camera_manager as cm
    monkeypatch.setattr(cm.camera_manager, "cameras", cams)
    monkeypatch.setattr(camera_profiles, "apply", lambda cameras, name: {
        "profile": name, "cameras": [{"cam_id": "c1"}], "unmatched": ["old_key"]})

    report = camera_profiles.apply_for_task("주간")
    joined = " ".join(report["warnings"])
    assert "손목" in joined and "기준" in joined, report["warnings"]
    assert "old_key" in joined, "장치를 못 찾은 프로파일 항목을 안 말했다"
