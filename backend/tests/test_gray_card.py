"""회색 카드 보정 (feature/gray-card-calibration.md).

정책은 색에 민감하다. 오전 데이터와 오후 추론이 화이트밸런스만 달라도 다른
관측이 되고, 탑뷰와 손목이 같은 물체를 다른 색으로 보면 정책은 그걸 **다른
물체의 특징으로** 배운다. 회색 카드는 거기에 재현 가능한 기준점을 준다.

여기서 잠그는 것은 **틀렸을 때 조용한 것들**이다: 못 믿을 측정으로 값을 정하는 것,
자동을 켜둔 채로 "보정했다"고 하는 것.
"""

import numpy as np
import pytest
from pathlib import Path

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

def test_awb_is_never_trusted_and_wb_is_measured_directly():
    """⚠ **AWB 의 수렴값은 읽을 수 없다** (실측 2026-09-02: 스트림을 켠 채
    수렴시켜도 읽기는 수동 설정값 그대로, 끄면 그 값으로 돌아온다). "켜서
    맞추고 꺼서 얼린다"는 WB 에서 거짓이었고, 그래서 모든 프로파일의 WB 가
    공장값 4600 이었다 — 조명이 4600K 에서 멀어질수록 화면이 치우쳤다(푸르게).

    새 계약: AWB 는 아예 켜지 않고, WB 는 카드의 R/B 균형으로 **직접 잰다.**
    """
    import inspect

    from piper_rs.hub import RealSenseHub

    src = inspect.getsource(RealSenseHub.calibrate_gray_card)
    assert '"enable_auto_white_balance", 1' not in src, \
        "AWB 를 다시 믿기 시작했다 — 수렴값이 읽히는지 실측부터 다시 하라"
    assert '"enable_auto_white_balance", 0' in src, "AWB 를 꺼두지 않는다"
    assert "white_balance_for" in src, "WB 를 직접 재서 맞추지 않는다"
    # 노출 동결은 실측으로 성립한다(프로파일들의 노출이 제각각) — AE 는 켰다 끈다
    assert '"enable_auto_exposure",' in src and '"enable_auto_exposure", 0' in src


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


def test_the_wheel_listens_on_the_layer_that_receives_the_mouse():
    """**회귀** — 휠이 아예 안 먹었다.

    마우스를 받는 층이 이미지를 덮고 있고 **둘은 형제**라, 이미지에 리스너를 달면
    wheel 이 거기까지 안 간다. 포인터를 받는 바로 그 요소에 달아야 한다.
    """
    from conftest import code_only

    src = (_SRC / "components" / "RoiPicker.tsx").read_text()
    assert "node.addEventListener('wheel'" in src, "휠을 상호작용 층에 안 단다"
    assert "img.addEventListener('wheel'" not in code_only(src), "이미지에 달면 안 먹는다"
    # 리스너를 다는 요소가 포인터도 받는 그 요소인가
    assert "ref={surfaceRef}" in src and "onPointerDown" in src.split("ref={surfaceRef}", 1)[1][:400]


def test_the_dimming_cannot_escape_the_preview():
    """**회귀** — `보정 시작` 을 누르면 **페이지 전체가 어두워졌다.**

    `shadow-[0_0_0_9999px_…]` 는 확산이 프리뷰 상자를 넘어 화면 끝까지 뻗는다.
    상자 둘레를 네 조각으로 덮으면 컨테이너 밖으로 샐 수가 없다.
    """
    from conftest import code_only

    # ⚠ 주석에도 `9999px` 가 나온다(왜 안 쓰는지 적어뒀다) — 주석을 걷어내고 본다
    src = code_only((_SRC / "components" / "RoiPicker.tsx").read_text())
    assert "9999px" not in src, "거대한 그림자가 페이지로 샌다"
    assert src.count("className={dim}") == 4, "상자 둘레를 네 조각으로 안 덮는다"


def test_the_box_survives_the_preview_reloading():
    """**회귀** — 휠을 굴리면 상자가 사라지거나 크기가 초기화됐다.

    설정 모달은 프리뷰 `src` 를 **200ms 마다** 갈아끼운다. 그때 `naturalWidth` 가
    0 이 되므로, 렌더마다 이미지에서 크기를 읽으면 상자가 5분의 1초마다 사라진다.
    마지막으로 제대로 읽은 값을 들고 있어야 한다.
    """
    src = (_SRC / "components" / "RoiPicker.tsx").read_text()
    assert "if (g) setGeo(g)" in src, "0 을 그대로 덮어쓴다 — 상자가 깜빡인다"
    assert "if (!nw || !nh || !r.width || !r.height) return null" in src, \
        "재로딩 중인 크기를 걸러내지 않는다"


def test_the_wheel_listener_is_attached_once():
    """**회귀** — 굴리는 내내 리스너가 떼였다 붙으면, 그 틈에 들어온 이벤트가
    **같은 옛 값에서 다시 계산**해 크기가 초기화된 것처럼 보인다."""
    src = (_SRC / "components" / "RoiPicker.tsx").read_text()
    wheel = src.split("node.addEventListener('wheel'", 1)[1]
    deps = wheel.split("}, [", 1)[1].split("]", 1)[0]
    assert "roi" not in deps and "onChange" not in deps, \
        f"휠 리스너가 값이 바뀔 때마다 다시 붙는다: [{deps}]"
    assert "roiRef.current" in src, "최신 값을 ref 로 안 읽는다"


def test_aiming_does_not_fire_a_request_per_wheel_tick():
    """휠 한 번에 이벤트가 수십 개 온다. 틱마다 보내면 조준하는 동안 요청이 밀려
    숫자가 뒤늦게, 뒤섞여 들어온다."""
    src = (_SRC / "pages" / "CamerasPage.tsx").read_text()
    body = src.split("const measureRoi", 1)[1].split("\n  }, [", 1)[0]
    assert "clearTimeout" in body and "setTimeout" in body, "디바운스가 없다"


def test_the_wheel_attaches_when_the_layer_appears_not_at_mount():
    """**회귀** — 휠이 아예 안 먹었다(두 번째).

    이 컴포넌트는 `geo` 가 잡히기 전에 `null` 을 렌더한다. 그때는 상호작용 층이
    **존재하지 않으므로**, 마운트 때 한 번 도는 이펙트는 붙일 대상을 못 찾고 끝난다 —
    의존성을 비워뒀으니 다시 시도하지도 않는다. 값이 바뀔 때 다시 붙게 하면
    이번엔 크기가 초기화되는 앞의 버그로 돌아간다.

    콜백 ref 는 요소가 **실제로 생기는 순간** 불린다 — 의존성을 맞출 필요가 없다.
    """
    src = (_SRC / "components" / "RoiPicker.tsx").read_text()
    assert "if (!roi || !geo) return null" in src, "전제가 바뀌었다 — 아래 판단을 다시 보라"
    assert "const surfaceRef = useCallback((node: HTMLDivElement | null)" in src, \
        "콜백 ref 가 아니면 층이 없는 동안 붙일 기회를 놓친다"
    assert "detachWheel" in src, "떼는 경로가 없다 — 리스너가 샌다"


# ── 보정 값이 언제 사라지는가 ────────────────────────────────────────────────

def test_the_profile_reapplies_on_every_connect():
    """⚠ **의도된 동작이다** (feature/gray-card-calibration.md §4: "저장은 안 한다
    — 프로파일이 한다"). 연결은 활성 프로파일의 컨트롤을 함께 넘긴다.

    문제는 그게 아니라 **말해주지 않은 것**이었다: 보정으로 좋은 값을 얻고
    [실시간 보기] 를 누르면 그 버튼이 `connect` 를 부르고, 프로파일 값이 다시
    걸려 "다시 어두워진다". 실측(D435, 프리셋 '주간 사무실'):

        보정 후     exposure = 8000
        실시간 보기 exposure = 93     ← 프리셋 값
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "backend" / "app" / "services"
           / "camera_manager.py").read_text()
    body = src.split("    def connect(self,", 1)[1].split("\n    def ", 1)[0]
    assert "_active_controls(self)" in body, "연결이 프로파일을 안 건다 — 설계가 바뀌었다"


def test_the_live_button_connects_first():
    """[실시간 보기] 가 `connect` 를 부르는 것이 위 경로의 방아쇠다."""
    from pathlib import Path

    page = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
            / "CamerasPage.tsx").read_text()
    live = page.split("'중단' : '실시간 보기'", 1)[0][-1200:]
    assert "/cameras/connect" in live


def test_the_result_box_says_it_is_not_saved():
    """⚠ 안내가 보정 **전** 문단에만 있었다 — 조준을 시작하면 사라졌다.
    결과를 본 직후가 저장이 가장 필요한 순간이다."""
    from pathlib import Path

    page = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
            / "CamerasPage.tsx").read_text()
    box = page.split("{grayCard && (", 1)[1].split("</div>", 1)[0]
    assert "장치에만" in box, "결과 상자가 임시값임을 말하지 않는다"
    assert "실시간 보기" in box, "무엇이 되돌리는지 말하지 않는다"


def test_the_modal_saves_nothing_and_points_to_the_profile_tab():
    """저장 버튼이 모달 안에 **있던** 적이 있다 — 그때는 프로파일 바가 페이지
    본문에 있어 모달(`fixed inset-0`)이 덮었기 때문이다. 프로파일 편집 **탭**이
    생기면서 저장 경로가 둘이 됐고, 사용자 결정(2026-09-02)으로 탭 하나만
    남겼다. 모달은 저장하지 않는다 — 대신 탭을 가리키고, 캡처가 **장치값**을
    읽으므로 창을 닫아도 값이 남는다는 것까지 말한다."""
    from pathlib import Path

    page = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
            / "CamerasPage.tsx").read_text()
    modal = page.split("{settingsCamera && (", 1)[1]
    assert "captureProfile" not in modal, \
        "모달에 저장 경로가 다시 생겼다 — 결정이 뒤집혔으면 이 테스트도 같이 고쳐라"
    assert "[프로파일] 탭에서 캡처" in modal, "저장이 어디로 갔는지 말하지 않는다"
    assert "fixed inset-0" in modal.split("\n", 2)[1], "모달이 아니라면 이 테스트를 다시 판단하라"


def test_the_hint_names_the_active_profile_when_there_is_one():
    """"프로파일 탭에서 캡처" 만으로는 **어디에 덮어쓸지** 알 수 없다 —
    활성이 있으면 그 이름을 대고, 없어도 문장이 성립해야 한다."""
    from pathlib import Path

    page = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
            / "CamerasPage.tsx").read_text()
    assert "activeProfile && ` (활성 '${activeProfile}'" in page


def test_the_active_name_is_readable_without_changing_it():
    """예전에는 POST 만 이름을 돌려줬다 — 알려면 바꿔야 했다."""
    router = (Path(__file__).resolve().parents[2] / "backend" / "app" / "routers"
              / "cameras.py").read_text()
    assert '@router.get("/profiles/active")' in router


def test_calibration_can_lock_exposure_and_adjust_gain_only():
    """노출은 모션 블러와 프레임 예산(긴 노출 = fps 상한)을 정한다 — 먼저 정해
    두고 밝기는 gain 으로만 잡는 선택지가 있어야 한다. gain 모드는 자동 노출을
    **아예 켜지 않는다**: 1단계에서 AE 를 켜는 순간 노출이 움직여 "고정"이
    거짓말이 된다."""
    root = Path(__file__).resolve().parents[2]
    hub = (root / "rs" / "piper_rs" / "hub.py").read_text()
    assert 'adjust: str = "exposure"' in hub, "손잡이 선택이 없다"
    assert '0 if adjust == "gain" else 1' in hub, "gain 모드가 AE 를 얼리지 않는다"

    page = (root / "frontend" / "src" / "pages" / "CamerasPage.tsx").read_text()
    assert "gainOnly ? 'gain' : 'exposure'" in page, "화면이 손잡이를 안 보낸다"
    assert "노출 고정" in page


def test_an_unknown_adjust_knob_is_rejected():
    """오타(`gian`)가 조용히 exposure 로 굴러가면 사용자는 노출이 고정된 줄
    알고 노출이 움직인 데이터를 찍는다 — 스키마가 거절한다."""
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).post("/api/cameras/x/calibrate-gray-card", json={"adjust": "iso"})
    assert r.status_code == 422


def test_zero_gain_is_a_value_not_a_missing_reading():
    """⚠ RealSense gain 은 **0 이 유효값**이다. `value or fallback` 패턴은
    0 을 결측으로 삼켜 16 으로 둔갑시킨다 — 비례 보정의 출발점이 틀어진다."""
    hub = (Path(__file__).resolve().parents[2] / "rs" / "piper_rs" / "hub.py").read_text()
    body = hub.split("def _control_range", 1)[1].split("def ", 1)[0]
    assert 'c.get("value") or' not in body, "or 패턴이 돌아왔다 — 0 을 삼킨다"
    assert "None" in body


def test_image_controls_accept_typed_numbers():
    """범위가 1~16000 인 노출을 슬라이더로 딱 맞추는 것은 손 떨림 게임이다 —
    숫자를 쳐 넣고 Enter 로 커밋할 수 있어야 하고, 범위 밖 입력은 클램프한다."""
    page = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
            / "CamerasPage.tsx").read_text()
    assert 'type="number"' in page and "onBlur" in page
    assert "Math.min(ctrl.max, Math.max(ctrl.min" in page, "클램프가 없다"


def test_calibration_refreshes_the_image_controls_below():
    """보정은 노출·gain·WB·자동 스위치를 전부 움직인다 — 아래 "이미지 조정"
    목록은 모달을 열 때 한 번만 읽으므로, 보정 직후 다시 읽지 않으면 옛 값이
    남아 "보정이 안 먹었나"로 읽힌다."""
    src = (_SRC / "pages" / "CamerasPage.tsx").read_text()
    body = src.split("const calibrateGrayCard", 1)[1].split("\n  }", 1)[0]
    assert "/controls`" in body and "setControls" in body, "보정 후 컨트롤을 안 다시 읽는다"


def test_wb_correction_moves_toward_neutral_and_is_damped():
    """푸른 카드(B>R)면 설정 K 를 올리고, 붉으면 내린다. 한 걸음의 배율을
    제한한다 — 반사로 튄 측정 한 번이 설정을 끝까지 밀면 안 된다."""
    blue = G.measure(card(118, b=140, r=100))
    up = G.white_balance_for(blue, 4600)
    assert up > 4600, "푸른데 K 를 안 올린다 — 방향이 뒤집혔다"

    red = G.measure(card(118, b=100, r=140))
    down = G.white_balance_for(red, 4600)
    assert down < 4600, "붉은데 K 를 안 내린다"

    # 극단 측정도 한 걸음은 1.6배 이내
    very_blue = G.measure(card(118, b=250, r=20))
    assert G.white_balance_for(very_blue, 4000) <= 4000 * 1.6 + 1


def test_wb_stays_inside_the_device_range():
    blue = G.measure(card(118, b=140, r=100))
    assert G.white_balance_for(blue, 6400, 2800, 6500) == 6500
    red = G.measure(card(118, b=100, r=140))
    assert G.white_balance_for(red, 2900, 2800, 6500) == 2800


def test_a_neutral_card_leaves_wb_alone():
    """맞아 있는 것을 흔들면 보정이 수렴하지 않는다."""
    neutral = G.measure(card(118))
    assert abs(G.white_balance_for(neutral, 4600) - 4600) < 20
