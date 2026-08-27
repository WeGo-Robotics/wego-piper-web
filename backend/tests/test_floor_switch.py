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


# ── 적용 범위 (화면이 거짓말하지 않는가) ──────────────────────────────────────

def test_the_panel_does_not_claim_to_cover_everything():
    """⚠ **실제로 틀렸던 문구다.** 두 가지를 놓쳤다:

    1. 파킹(`arm.go_parking`)과 말단 조그(`arm.jog_end_pose`)는 `filter_goal` 을
       안 지나고 `JointCtrl`/`EndPoseCtrl` 을 **직접** 부른다
    2. LeRobot 녹화·추론이 걸리는지는 `robot_transport` 가 정한다 —
       `direct` 면 subprocess 가 CAN 을 직접 연다. **코드 기본값이 `direct` 다**

    안전 화면이 안 걸리는 것을 걸린다고 말하는 것이 여기서 제일 나쁜 고장이다.
    """
    # ⚠ `code_only` 로 주석을 걷어낸다 — 왜 그 문구를 안 쓰는지 설명하는 주석이
    #   그 문구를 금지하는 검사에 걸린다. 이 저장소에서 네 번째다.
    assert "전부에 걸립니다" not in code_only(_src())
    assert "Coverage" in _src(), "적용 범위를 보여주는 곳이 없다"


def test_coverage_follows_the_actual_transport():
    """`direct` 로 바꾸면 녹화·추론 줄이 꺼져야 한다 — 화면이 설정을 읽어야 한다."""
    src = _src()
    assert "transport === 'shm'" in src
    body = src.split("function Coverage", 1)[1].split("\n}", 1)[0]
    assert body.count("shm,") >= 2, "녹화와 추론이 전송 방식에 안 걸려 있다"


def test_the_uncovered_paths_are_named():
    """안 걸리는 것은 **이름과 이유를 같이** 적는다 — 목록에서 빠지면
    걸리는 줄 안다."""
    body = _src().split("function Coverage", 1)[1].split("\n}", 1)[0]
    for name in ("파킹", "말단"):
        assert name in body, f"{name} 이 적용 범위 목록에 없다"
    assert "온보드 IK" in body


def test_the_endpoint_reports_the_transport():
    """화면이 추측하지 않게 서버가 알려준다."""
    router = (Path(__file__).resolve().parents[1] / "app" / "routers" / "robots.py").read_text()
    assert '"transport": settings.robot_transport' in router
