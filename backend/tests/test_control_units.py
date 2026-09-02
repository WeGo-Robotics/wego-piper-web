"""컨트롤 표시 단위 — "노출 266" 이 뭔지 화면이 말해준다.

v4l2 의 `exposure_time_absolute` 는 **100µs 단위**(V4L2 규격)다. RealSense 의
`exposure` 는 **센서마다 단위가 다르다** (실측 2026-09-02, 이 기계의 장치들):

- D435 RGB 센서: max 10000 → **100µs 단위** (값 266 = 26.6ms. µs 로 읽으면
  최대 노출이 10ms 라는 뜻이 되는데, 그런 카메라는 없다)
- D405 컬러·깊이(스테레오 모듈): max 165000 → **µs**

이름만 보고 단위를 정하면 D435 에서 100배 틀린다 — **범위가 지문이다.**
지식은 `piper_cam.controls` 한 곳, 부착은 게이트웨이(`camera_manager`).
"""

from pathlib import Path
from types import SimpleNamespace

from piper_cam.controls import exposure_unit_scale, unit_for

from app.services.camera_manager import camera_manager


def test_the_range_tells_the_exposure_unit_apart():
    assert exposure_unit_scale(10000) == 100      # D435 계열 RGB
    assert exposure_unit_scale(165000) == 1       # 스테레오 모듈 (D405 컬러 포함)
    assert exposure_unit_scale(None) == 1         # 모르면 환산하지 않는다


def test_unit_labels_follow_the_sensor_not_the_name():
    assert unit_for({"name": "exposure", "max": 10000}) == "×100µs"
    assert unit_for({"name": "exposure", "max": 165000}) == "µs"
    assert unit_for({"name": "exposure_time_absolute"}) == "×100µs"   # v4l2 — 규격 고정
    # ⚠ 모르는 단위를 지어내면 틀린 단위가 된다 — 없는 단위보다 나쁘다
    assert unit_for({"name": "gain", "max": 128}) is None


def test_the_gateway_attaches_units_per_control(monkeypatch):
    cam = SimpleNamespace(get_controls=lambda: [
        {"name": "exposure", "value": 266, "max": 10000},     # D435 color
        {"name": "exposure", "value": 8500, "max": 165000},   # D405/깊이
        {"name": "gain", "value": 10, "max": 128},
    ])
    monkeypatch.setattr(camera_manager, "cameras", {"c1": cam})
    out = camera_manager.get_controls("c1")
    assert out[0]["unit"] == "×100µs"
    assert out[1]["unit"] == "µs"
    assert "unit" not in out[2]


def test_gray_card_report_converts_exposure_to_true_us():
    """⚠ rsd 의 `exposure_us` 필드가 D435 에서는 원시값(100µs 단위)을 그대로
    담아 **100배 거짓말**을 했다 — 화면이 21.6ms 를 0.2ms 로 보여줬다.
    보고 직전에 범위로 환산해야 한다. 장치에 쓰는 값은 원시값 그대로다."""
    hub = (Path(__file__).resolve().parents[2] / "rs" / "piper_rs" / "hub.py").read_text()
    body = hub.split("def calibrate_gray_card", 1)[1]
    assert "exposure_unit_scale" in body, "보고를 환산하지 않는다"
    assert "exposure_unit_scale(exp_hi)" in body, "최종 보고가 범위를 안 본다"


def test_the_settings_modal_renders_the_unit_and_translates_to_ms():
    """단위 기호만으로는 부족하다 — "×100µs" 를 본 사람이 암산하게 두지 않고
    툴팁이 ms 로 번역해 준다 (266 × 100µs = 26.6ms)."""
    page = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
            / "CamerasPage.tsx").read_text()
    assert "ctrl.unit" in page, "화면이 단위를 안 그린다"
    assert "unitHint" in page and "ms" in page, "ms 번역 툴팁이 없다"
