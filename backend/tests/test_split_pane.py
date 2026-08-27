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
