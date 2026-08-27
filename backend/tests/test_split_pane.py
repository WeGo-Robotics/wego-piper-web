"""좌우 폭을 사용자가 끌어서 조절한다 (에피소드 보기).

⚠ 이 화면에서 **CSS 와 JS 가 같은 조건을 따로 판단하다 어긋난 전례**가 있다.
칸을 나누는 기준은 `2xl` 브레이크포인트인데 카메라 배치는 `layout` 상태만 봐서,
좁은 화면에서 한 칸인데 카메라만 나란히 놓였다. 폭 조절은 인라인 스타일이라
같은 실수를 하면 더 나쁘다 — 접혀야 할 화면에 폭이 박힌 채 남는다.
"""

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
_PANE = _SRC / "components" / "SplitPane.tsx"
_PAGE = _SRC / "pages" / "EpisodesPage.tsx"


def test_the_breakpoint_is_read_in_js_not_guessed():
    """인라인 폭은 CSS 미디어쿼리로 못 끈다 — JS 가 같은 값을 봐야 한다."""
    page = _PAGE.read_text()
    assert "useMediaQuery('(min-width: 1536px)')" in page, "브레이크포인트를 JS 로 안 본다"
    assert "2xl" in page or "1536" in page


def test_nothing_is_forced_when_the_screen_is_one_column():
    """⚠ 한 칸으로 접혀야 할 화면에 폭·flex 가 남으면 내용이 찌그러진다."""
    page = _PAGE.read_text()
    assert "splitOn ? { width:" in page, "폭이 조건 없이 걸린다"
    assert "splitOn ? 'flex items-start' : 'space-y-3'" in page, "컨테이너가 안 접힌다"


def test_the_split_is_clamped():
    """한쪽이 너무 좁아지면 내용이 읽히지 않는다 — 끝까지 못 끌게 막는다."""
    src = _PANE.read_text()
    lo = int(re.search(r"MIN_PCT\s*=\s*(\d+)", src).group(1))
    hi = int(re.search(r"MAX_PCT\s*=\s*(\d+)", src).group(1))
    assert 5 <= lo < 50 < hi <= 95
    assert "Math.min(MAX_PCT, Math.max(MIN_PCT" in src, "클램프를 안 쓴다"


def test_the_stored_value_is_validated_on_read():
    """localStorage 는 사람이 고칠 수 있고 예전 형식이 남을 수도 있다 —
    범위 밖 값이 들어오면 화면이 한쪽으로 무너진다."""
    src = _PANE.read_text()
    body = src.split("export function useSplit", 1)[1].split("\n}", 1)[0]
    assert "Number.isFinite" in body and "MIN_PCT" in body, "읽은 값을 안 거른다"


def test_dragging_does_not_write_storage_every_frame():
    """⚠ 끄는 내내 저장하면 프레임마다 localStorage 를 친다."""
    src = _PANE.read_text()
    drag = src.split("onPointerMove", 1)[1].split("onPointerUp", 1)[0]
    assert "setItem" not in drag and "onCommit" not in drag, "끌면서 저장한다"
    up = src.split("onPointerUp", 1)[1].split("onDoubleClick", 1)[0]
    assert "onCommit" in up, "놓을 때 저장을 안 한다"


def test_the_drag_releases_the_text_selection_lock():
    """`userSelect` 를 끄고 안 되돌리면 **페이지 전체에서 글자를 못 고른다.**"""
    src = _PANE.read_text()
    assert src.count("document.body.style.userSelect") == 2, "켜거나 끄는 쪽이 빠졌다"
    assert "userSelect = ''" in src, "선택 잠금이 안 풀린다"


def test_there_is_a_way_back_to_even():
    """끌어서 망가뜨렸을 때 되돌릴 길이 있어야 한다."""
    assert "onDoubleClick={onReset}" in _PANE.read_text()


# ── 리사이즈·드래그에 그래프가 사라지던 문제 ────────────────────────────────

_CHART = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "PlotlyChart.tsx"


def test_the_chart_watches_its_container_not_just_the_window():
    """⚠ Plotly 의 `responsive` 는 **창 리사이즈 이벤트**만 듣는다.

    분할바를 끌면 컨테이너 폭만 바뀌고 그 이벤트는 안 난다 — Plotly 는 옛 픽셀
    폭 그대로 남고, 한 번이라도 폭 0 으로 잡히면 빈 채로 굳는다. 그래프가
    사라져 보이던 원인이다.
    """
    src = _CHART.read_text()
    assert "ResizeObserver" in src, "컨테이너를 안 본다"
    assert "Plots.resize" in src, "관찰만 하고 다시 그리지 않는다"


def test_resizing_is_batched_and_skips_zero_width():
    """리사이즈 중에는 초당 수십 번 온다. 그리고 폭 0 에서 부르면 Plotly 가
    0 크기로 자리를 잡아 버린다."""
    src = _CHART.read_text()
    body = src.split("new ResizeObserver", 1)[1].split("ro.observe", 1)[0]
    assert "requestAnimationFrame" in body and "cancelAnimationFrame" in body, "묶지 않는다"
    assert "clientWidth > 0" in body, "폭 0 에서도 부른다"


def test_dragging_does_not_re_render_the_page():
    """⚠ 폭을 상태로 두면 포인터 이벤트마다 페이지가 다시 그려지고, 그래프
    열 몇 개가 매 프레임 재생성된다 — 무겁고, 그리는 도중 폭이 0 으로 잡히면
    빈 채로 굳는다."""
    pane = _PANE.read_text()
    move = pane.split("const move = ", 1)[1].split("return (", 1)[0]
    assert "setProperty('--split'" in move, "CSS 변수로 안 쓴다"
    assert "onDrag" not in pane, "끌면서 상태를 올린다"

    page = _PAGE.read_text()
    assert "'--split'" in page, "페이지가 CSS 변수를 안 받는다"
    assert "width: 'var(--split)'" in page, "폭이 변수에 안 묶였다"


def test_pressing_without_moving_keeps_the_current_width():
    """⚠ 커밋 값이 초기값(50%)에서 시작하면 **누르기만 해도** 배치가 튄다.

    끌 생각 없이 손잡이를 건드린 것뿐인데 폭이 돌아가 버린다.
    """
    src = _PANE.read_text()
    down = src.split("onPointerDown", 1)[1].split("onPointerMove", 1)[0]
    assert "last.current =" in down, "지금 폭에서 시작하지 않는다"


def test_releasing_wakes_plotly_even_if_the_observer_missed_it():
    """⚠ 관찰이 어떤 이유로든 놓치면 그래프가 옛 폭으로 굳고 **되돌아올 계기가 없다.**

    창 리사이즈 이벤트는 Plotly 자신의 `responsive` 경로를 깨우므로, 놓는 순간
    한 번 쏴서 안전망을 둔다.
    """
    src = _PANE.read_text()
    up = src.split("onPointerUp", 1)[1].split("onDoubleClick", 1)[0]
    assert "new Event('resize')" in up, "안전망이 없다"


def test_a_failed_resize_is_reported():
    """조용히 삼키면 **그래프가 안 그려진 이유가 어디에도 안 남는다.**"""
    src = _CHART.read_text()
    body = src.split("Plots.resize", 1)[1][:400]
    assert "console.warn" in body, "실패를 삼킨다"


def test_an_empty_chart_reports_itself():
    """⚠ "그래프 몇 개가 안 보인다" 를 어느 조건에서도 재현하지 못했다 —
    원격 주소·GPU·같은 뷰포트까지 맞춰 헤드리스로 11/11 이었다.

    재현이 안 되면 화면이 대신 말해줘야 한다. **콘솔에 아무것도 안 뜨는 것**이
    지금 원인 추적의 가장 큰 벽이다.
    """
    src = _CHART.read_text()
    assert "가 비어 있습니다" in src, "빈 그래프가 조용히 넘어간다"
    body = src.split("가 비어 있습니다", 1)[1][:300]
    for key in ("컨테이너폭", "plotly생성됨", "페이지전체그래프"):
        assert key in body, f"{key} 를 안 알려준다 — 원인 구분이 안 된다"
