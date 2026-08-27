"""바닥 필터 켜기/끄기 스위치 (설정-안전).

버튼이 아니라 슬라이드 스위치다. 안전 스위치라서 **상태가 색 하나로 읽혀야**
하고, 마우스 없이도 켜져야 한다.
"""

from pathlib import Path

from conftest import code_only

PANEL = (Path(__file__).resolve().parents[2]
         / "frontend" / "src" / "components" / "FloorGuardPanel.tsx")


def _src() -> str:
    return PANEL.read_text()


def test_the_switch_is_a_real_switch():
    """⚠ `<div onClick>` 으로 만들면 **키보드로 못 켜고** 스크린리더가 상태를
    못 읽는다. 안전 스위치라 더 그렇다."""
    src = _src()
    assert 'role="switch"' in src
    assert "aria-checked={on}" in src
    assert 'type="button"' in src, "폼 안에서 submit 이 되면 안 된다"


def test_colour_marks_on_not_off():
    """켜졌을 때만 색이다. 꺼짐이 색을 갖거나 둘 다 무채색이면 한눈에 안 읽힌다."""
    src = code_only(_src())
    track = src.split("rounded-full\n        transition-colors", 1)[1].split("`}", 1)[0]
    assert "on ? 'bg-green-500' : 'bg-neutral-600'" in track.replace("\n", " ").replace("  ", " ")


def test_the_knob_actually_slides():
    """미끄러지지 않으면 그냥 색만 바뀌는 사각형이다.

    ⚠ Tailwind v4 는 `translate-x-*` 를 `transform` 이 아니라 **`translate`
      속성**으로 낸다. `transition-transform` 이 v4 에서 그걸 함께 덮으므로
      맞는 클래스다 — 실측 `transition-property: transform, translate, scale,
      rotate`, 0.15s. `transition-colors` 로 바꾸면 손잡이가 순간이동한다.
    """
    src = _src()
    knob = src.split("<span", 1)[1].split("/>", 1)[0]
    assert "translate-x-6" in knob and "translate-x-1" in knob
    assert "transition-transform" in knob


def test_the_switch_is_disabled_while_the_request_is_in_flight():
    """연타하면 마지막 것이 이기는데, 그 사이 화면은 중간 상태를 보여준다."""
    assert "disabled={busy}" in _src()


def test_the_state_is_also_written_out():
    """색만으로는 색각 이상에서 안 읽힌다 — 글자도 같이 낸다."""
    src = _src()
    assert "'켜짐' : '꺼짐'" in src
