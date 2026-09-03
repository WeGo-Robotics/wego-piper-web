"""카메라 카드의 노출 표시 — 눈금(EV) · 셔터 · 게인.

밝기는 shm 프레임에서 공짜로 재지만 노출·게인은 **장치를 물어야** 안다.
RealSense UVC 질의가 D405 를 커널 D-state 로 물린 전례가 있어서, 카드마다
질의하는 대신 샘플러가 느린 주기로 한 번 읽어 실어 보낸다.
"""

import math
from pathlib import Path

import pytest
from conftest import code_only
from piper_cam.controls import exposure_us
from piper_cam.graycard import TARGET_LUMA
from piper_cam.lighting import EV_LIMIT, METERING_MODES, ev, features

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
READOUT = _SRC / "components" / "ExposureReadout.tsx"


# ── 눈금 ────────────────────────────────────────────────────────────────────

def test_zero_means_what_the_gray_card_calibration_means():
    """⚠ 0 점이 회색카드 목표와 다르면 화면 두 곳이 같은 카메라를 두고 다른 말을
    한다 — "0.0 EV" 와 "보정 완료" 가 서로 다른 밝기를 뜻하게 된다."""
    assert ev(TARGET_LUMA) == 0.0


def test_one_stop_is_one_doubling():
    """스톱은 곱셈 눈금이다. 노출을 두 배 주면 luma 도 두 배라 '+1' 이 언제나
    같은 크기의 조작을 뜻한다."""
    assert ev(TARGET_LUMA * 2) == 1.0
    assert ev(TARGET_LUMA / 2) == -1.0
    assert ev(TARGET_LUMA * 4) == 2.0


def test_the_scale_has_ends_and_darkness_does_not_explode():
    """⚠ log₂(0) 은 -∞ 다. 완전 암흑에서 눈금이 터지면 화면이 깨진다."""
    assert ev(0.0) == -EV_LIMIT
    assert ev(-1.0) == -EV_LIMIT
    assert -EV_LIMIT <= ev(255.0) <= EV_LIMIT
    assert ev(TARGET_LUMA * 1000) == EV_LIMIT


def test_the_frame_measurement_carries_every_mode():
    """⚠ 셋을 **다 실어 보낸다.** 고른 하나만 보내면 모드를 바꿀 때마다 다음
    샘플(2초)을 기다려야 하고, 무엇보다 비교가 안 된다 — 측광이 수상할 때
    사람이 제일 먼저 하는 일이 모드를 바꿔 보는 것이다."""
    import numpy as np

    feats = features(np.full((32, 32, 3), 118, dtype=np.uint8))
    assert set(feats["ev"]) == set(METERING_MODES), feats["ev"]
    assert set(feats["metering"]) == set(METERING_MODES), feats["metering"]
    for m in METERING_MODES:
        assert abs(feats["ev"][m]) < 0.2, (m, feats["ev"])


def test_the_modes_actually_look_at_different_places():
    import numpy as np

    frame = np.zeros((64, 64, 3), np.uint8)
    frame[20:44, 20:44] = 200                 # 가운데만 밝다
    m = features(frame)["metering"]
    assert m["spot"] > m["center"] > m["average"], m


def test_every_mode_shares_one_target():
    """⚠ 목표가 모드마다 다르면 같은 '+1.0 EV' 가 모드마다 다른 뜻이 되어
    모드를 바꿔 비교하는 일 자체가 무의미해진다. 바뀌는 건 '어디를 재나' 뿐이다."""
    import numpy as np

    feats = features(np.full((64, 64, 3), int(TARGET_LUMA), dtype=np.uint8))
    for m in METERING_MODES:
        assert abs(feats["ev"][m]) < 0.05, (m, feats["ev"])


def test_the_alert_baseline_is_not_the_metering_mode():
    """⚠ Judge 의 급변 판정은 `luma` 를 기준선으로 쓴다. 사람이 고른 측광 모드에
    따라 그 뜻이 바뀌면 **표시를 바꾸려다 안전 경보를 건드리는** 셈이다."""
    import numpy as np

    frame = np.zeros((64, 64, 3), np.uint8)
    frame[20:44, 20:44] = 200
    feats = features(frame)
    assert feats["luma"] == feats["metering"]["average"], "luma 가 평균이 아니다"

    src = code_only(Path(__file__).resolve().parents[2]
                    .joinpath("cam/piper_cam/lighting.py").read_text())
    judge = src.split("class Judge", 1)[1]
    assert "metering" not in judge and '"ev"' not in judge, "판정이 측광 모드를 본다"


def test_the_front_end_uses_the_same_ends():
    """⚠ 눈금 끝이 다르면 마커 위치가 값과 어긋난다 — 숫자는 +5 인데 막대는
    가운데를 가리키는 식이다."""
    src = code_only(READOUT.read_text())
    assert f"const EV_LIMIT = {int(EV_LIMIT)}" in src, "프론트의 눈금 끝이 백엔드와 다르다"


# ── 노출 단위 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ctrl,want", [
    # RealSense RGB 센서 — max 10000 이면 ×100µs 다 (266 = 26.6ms)
    ({"name": "exposure", "value": 266, "max": 10000}, 26600.0),
    # D405 컬러·스테레오 — max 165000 이면 그냥 µs
    ({"name": "exposure", "value": 8300, "max": 165000}, 8300.0),
    # v4l2 는 이름이 곧 단위다 (×100µs)
    ({"name": "exposure_time_absolute", "value": 83}, 8300.0),
    ({"name": "exposure_absolute", "value": 83}, 8300.0),
])
def test_every_exposure_name_lands_in_microseconds(ctrl, want):
    """⚠ 같은 '노출' 이 센서마다 단위가 다르다. 호출부마다 환산을 다시 적으면
    언젠가 100배 틀린 값이 화면에 뜨는데, 그건 눈으로 못 거른다."""
    assert exposure_us(ctrl) == want


def test_things_that_are_not_exposure_stay_out():
    assert exposure_us({"name": "gain", "value": 47}) is None
    assert exposure_us({"name": "exposure", "value": None, "max": 10000}) is None


# ── 샘플러 ──────────────────────────────────────────────────────────────────

class _Cam:
    id, label, name, connected = "cam0", "왼손목", "cam0", True

    def __init__(self, controls, boom=False):
        self._controls, self._boom, self.calls = controls, boom, 0

    def get_controls(self):
        self.calls += 1
        if self._boom:
            raise RuntimeError("장치 질의 실패")
        return self._controls


def test_the_knobs_are_read_slower_than_the_brightness():
    """⚠ 밝기는 shm 에서 공짜지만 이건 장치 질의다 — 매 샘플마다 물으면 안 된다."""
    from app.services.light_watch import KNOBS_EVERY_S, LightWatch

    cam = _Cam([{"name": "exposure", "value": 100, "max": 10000},
                {"name": "gain", "value": 47}])
    w = LightWatch(bus=False)
    assert w._knobs_for(cam, 0.0) == {"exposure_us": 10000.0, "gain": 47}
    w._knobs_for(cam, KNOBS_EVERY_S / 2)          # 아직 캐시
    assert cam.calls == 1, "샘플마다 장치를 묻는다"
    w._knobs_for(cam, KNOBS_EVERY_S + 0.1)
    assert cam.calls == 2, "영영 갱신을 안 한다"


def test_a_failed_query_keeps_the_last_known_value():
    """⚠ 모르는 것과 없는 것은 다르다. 타임아웃으로 숫자가 사라지면 사람은
    "노출이 0 이 됐나" 로 읽는다."""
    from app.services.light_watch import KNOBS_EVERY_S, LightWatch

    w = LightWatch(bus=False)
    good = _Cam([{"name": "exposure", "value": 100, "max": 10000}])
    assert w._knobs_for(good, 0.0)["exposure_us"] == 10000.0

    dead = _Cam([], boom=True)
    dead.id = good.id
    assert w._knobs_for(dead, KNOBS_EVERY_S + 1)["exposure_us"] == 10000.0

    empty = _Cam([])          # 빈 목록도 "못 읽음" 이다 (rsd 의 타임아웃 기본값)
    empty.id = good.id
    assert w._knobs_for(empty, 2 * KNOBS_EVERY_S + 2)["exposure_us"] == 10000.0


def test_the_card_does_not_ask_the_device_itself():
    """⚠ 카메라 카드마다 컨트롤을 질의하면 D405 를 D-state 로 물린다.
    표시는 샘플러가 재 둔 것을 받아 쓰기만 한다."""
    src = code_only(READOUT.read_text())
    assert "/controls" not in src and "api.get" not in src, "표시 컴포넌트가 장치를 묻는다"

    page = code_only((_SRC / "pages" / "CamerasPage.tsx").read_text())
    assert page.count("'/cameras/light'") == 1, "폴링이 한 곳이 아니다"


def test_clipping_is_called_out_because_no_mode_fixes_it():
    """⚠ 잘린 화소는 자기가 원래 얼마나 밝았는지 **말할 수 없다.** 그래서 포화가
    심하면 측광값이 실제보다 낮게 나오는데, 이건 모드를 바꿔도 안 고쳐진다 —
    "측광이 이상하다" 의 실제 원인이 대개 이것이라 화면이 말해 줘야 한다."""
    src = code_only(READOUT.read_text())
    assert "sat_pct" in src, "포화를 안 본다"
    assert "SAT_UNRELIABLE_PCT" in src, "포화 임계가 값에 묻혀 있다"


def test_the_mode_is_a_way_of_looking_not_a_device_setting():
    """⚠ 측광 모드로 장치를 만지면 안 된다 — 카메라가 실제로 노출을 바꾸는 것과
    "어떻게 읽을까" 는 다른 일이다. 브라우저에 남기고 장치는 그대로 둔다."""
    page = code_only((_SRC / "pages" / "CamerasPage.tsx").read_text())
    assert "localStorage.setItem('piper_metering'" in page, "고른 모드가 안 남는다"
    seg = page.split("setMetering", 1)[1][:400]
    assert "api.post" not in seg, "모드를 바꾸며 장치를 만진다"
