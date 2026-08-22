"""회색 카드 보정 (feature/gray-card-calibration.md).

정책은 색에 민감하다. 오전 데이터와 오후 추론이 화이트밸런스만 달라도 다른
관측이 되고, 탑뷰와 손목이 같은 물체를 다른 색으로 보면 정책은 그걸 **다른
물체의 특징으로** 배운다. 회색 카드는 거기에 재현 가능한 기준점을 준다.

여기서 잠그는 것은 **틀렸을 때 조용한 것들**이다: 못 믿을 측정으로 값을 정하는 것,
자동을 켜둔 채로 "보정했다"고 하는 것.
"""

import numpy as np
import pytest

pytest.importorskip("piper_cam")
from piper_cam import graycard as G  # noqa: E402


def card(value=118, shape=(480, 640), r=None, g=None, b=None, noise=0.0):
    img = np.full((*shape, 3), value, np.float32)
    for i, v in enumerate((b, g, r)):
        if v is not None:
            img[..., i] = v
    if noise:
        rng = np.random.default_rng(0)
        img += rng.normal(0, noise, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def test_a_neutral_card_at_target_passes():
    ok, why = G.measure(card()).verdict()
    assert ok, why


def test_a_colour_cast_is_reported_as_a_cast_not_as_brightness():
    """WB 와 노출은 **따로** 보여야 한다 — 섞으면 무엇을 고칠지 못 말한다."""
    r = G.measure(card(118, r=140))
    ok, why = r.verdict()
    assert not ok
    assert "치우침" in why and "밝기" not in why


def test_brightness_off_target_is_reported_as_brightness():
    r = G.measure(card(60))
    ok, why = r.verdict()
    assert not ok and "밝기" in why


def test_a_saturated_card_is_refused_not_measured():
    """⚠ 포화된 카드로 정한 노출은 다음 주에 재현되지 않는다 — 그게 이 기능의
    존재 이유인데 스스로 깨는 셈이다."""
    ok, why = G.measure(card(255)).usable
    assert not ok and "포화" in why


def test_an_uneven_card_is_refused():
    """그림자나 반사가 걸린 카드는 못 믿는다."""
    img = card(118)
    img[:, :320] = 40                     # 절반이 그늘
    ok, why = G.measure(img).usable
    assert not ok and "고르지" in why


def test_exposure_correction_moves_toward_the_target():
    r = G.measure(card(59))               # 목표의 절반
    new = G.exposure_for(r, 10000, 1, 165000)
    assert 19000 < new < 21000, new


def test_a_black_frame_does_not_slam_the_exposure_to_maximum():
    """**회귀 방지** — 렌즈가 막혔거나 조명이 꺼진 상태에서 비례 보정을 그대로
    믿으면 노출을 최대까지 밀어붙이고, 치우는 순간 다음 프레임이 새하얗게 된다."""
    r = G.measure(card(0))
    assert G.exposure_for(r, 10000, 1, 165000) <= 20000


def test_exposure_stays_inside_the_device_range():
    r = G.measure(card(10))
    assert G.exposure_for(r, 10000, 1, 12000) == 12000
    r2 = G.measure(card(250))
    assert G.exposure_for(r2, 10000, 9000, 165000) == 9000


def test_the_centre_box_is_where_the_lens_is_most_even():
    """가장자리는 비네팅이 있다 — 거기서 재면 카드가 실제보다 어둡게 보인다."""
    x, y, w, h = G.center_roi((480, 640), 0.3)
    assert (x, y, w, h) == (224, 168, 192, 144)


def test_noise_alone_does_not_fail_a_good_card():
    """센서 잡음은 정상이다. 여기서 걸리면 아무도 못 통과한다."""
    ok, why = G.measure(card(118, noise=3.0)).verdict()
    assert ok, why


# ── 데몬 절차 ────────────────────────────────────────────────────────────────

def test_the_calibration_turns_auto_off_afterwards():
    """⚠ 자동을 켜둔 채로는 **카드를 치우는 순간 다시 흔들린다.**

    잠그는 것이 "보정"의 실체다. 자동으로 맞추게 두는 것은 방법일 뿐이다.
    """
    import inspect

    from piper_rs.hub import RealSenseHub

    src = inspect.getsource(RealSenseHub.calibrate_gray_card)
    on = src.index('"enable_auto_white_balance", "enable_auto_exposure"')
    off = src.index('"enable_auto_white_balance", "enable_auto_exposure"', on + 1)
    assert ", 1)" in src[on:on + 200], "자동을 안 켜고 잰다"
    assert ", 0)" in src[off:off + 200], "자동을 안 끈다 — 카드를 치우면 도로 흔들린다"


def test_an_unusable_reading_stops_before_changing_anything():
    """못 믿을 측정으로 노출을 정하면 재현이 안 되는 값이 장치에 남는다."""
    import inspect

    from piper_rs.hub import RealSenseHub

    src = inspect.getsource(RealSenseHub.calibrate_gray_card)
    head = src.split("# 2) 자동을 끈다", 1)[0]
    assert "before.usable" in head and "return" in head, "못 믿는 측정에도 진행한다"


def test_calibration_is_blocked_while_a_camera_is_in_use():
    """도중에 노출이 바뀌면 한 에피소드 안에서 밝기가 달라지고,
    정책은 그걸 장면 변화로 배운다."""
    import inspect

    from app.routers import cameras

    assert "require_idle" in inspect.getsource(cameras.calibrate_gray_card)


# ── 영역 고르기 (프론트) ─────────────────────────────────────────────────────

from pathlib import Path  # noqa: E402

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def test_the_roi_is_mapped_through_the_drawn_image_not_the_element():
    """⚠ **`object-contain` 은 레터박스를 만든다.**

    848x480 프레임을 `aspect-[4/3]` 상자에 넣으면 위아래에 빈 띠가 생긴다.
    요소 좌표를 그대로 비율로 쓰면 그 띠만큼 어긋나 **상자가 손끝에서 미끄러진다.**
    """
    src = (_SRC / "components" / "RoiPicker.tsx").read_text()
    assert "naturalWidth" in src and "naturalHeight" in src, "원본 프레임 크기를 안 본다"
    assert "Math.min(r.width / nw, r.height / nh)" in src, "contain 맞춤을 안 푼다"


def test_the_wheel_listener_is_not_passive():
    """React 의 `onWheel` 은 루트에 passive 로 붙어 `preventDefault()` 가 안 먹는다 —
    그러면 상자를 키우는 동안 **설정 모달이 같이 스크롤된다.**"""
    src = (_SRC / "components" / "RoiPicker.tsx").read_text()
    assert "addEventListener('wheel'" in src, "네이티브 리스너를 안 쓴다"
    assert "{ passive: false }" in src, "passive 로 붙어 preventDefault 가 안 먹는다"
    assert "onWheel=" not in src, "React onWheel 로 되돌아갔다"


def test_the_box_never_leaves_the_frame():
    """프레임 밖을 자르면 백엔드가 빈 ROI 를 받는다."""
    src = (_SRC / "components" / "RoiPicker.tsx").read_text()
    box = src.split("export function toBox", 1)[1].split("\n}", 1)[0]
    assert "Math.max" in box and "Math.min" in box, "가장자리에서 안 막는다"
    assert "MIN_SIZE" in box, "최소 크기를 안 지킨다"


def test_the_calibration_sends_the_chosen_box():
    """상자를 골라놓고 안 보내면 사용자는 **가운데를 잰 결과**를 보게 된다."""
    src = (_SRC / "pages" / "CamerasPage.tsx").read_text()
    body = src.split("const calibrateGrayCard", 1)[1].split("\n  }", 1)[0]
    assert "toBox(" in body and "roi: box" in body, "고른 영역을 안 싣는다"


def test_aiming_the_box_does_not_touch_the_device():
    """상자를 옮길 때마다 부르는 경로다 — 여기서 컨트롤을 건드리면 조준하는 동안
    노출이 춤춘다."""
    import inspect

    from piper_rs.hub import RealSenseHub

    src = inspect.getsource(RealSenseHub.measure_gray_card)
    assert "set_control" not in src, "재기만 해야 하는데 장치를 건드린다"
    assert "sleep" not in src, "조준 되먹임이 느려진다"


def test_the_box_is_hidden_until_calibration_starts():
    """프리뷰는 대부분의 시간 **카메라를 확인하는 화면**이다.

    늘 떠 있는 조준 상자는 그때 방해만 되고, 지금 보정 중인지 아닌지도 흐려진다.
    """
    src = (_SRC / "pages" / "CamerasPage.tsx").read_text()
    assert "{aiming && settingsCamera.stream_type !== 'depth' && (\n                <RoiPicker" in src, \
        "조준 중이 아닐 때도 상자를 그린다"


def test_starting_calibration_measures_once_right_away():
    """빈 상자만 뜨면 어디로 옮겨야 좋은지 알 수 없다 — 첫 숫자를 바로 채운다."""
    src = (_SRC / "pages" / "CamerasPage.tsx").read_text()
    body = src.split("const startAiming", 1)[1].split("\n  }", 1)[0]
    assert "setAiming(true)" in body and "measureRoi(" in body


def test_finishing_calibration_puts_the_box_away():
    """결과를 볼 차례다. 상자가 남아 있으면 아직 조준 중인 것처럼 보인다."""
    src = (_SRC / "pages" / "CamerasPage.tsx").read_text()
    body = src.split("const calibrateGrayCard", 1)[1].split("\n  }", 1)[0]
    assert "setAiming(false)" in body
