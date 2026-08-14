"""페이지 목록은 `pages.ts` 한 곳에서만 나온다 (feature/layout-redesign.md §3).

`config/pages.ts` 는 스스로를 *"페이지 정의 단일 소스"* 라고 적어 두었고,
라우트·내비·대시보드 카드가 전부 여기서 파생된다. **그런데 그걸 지키는 것이
지금까지 주석뿐이었다** — 사이드바가 자기 목록을 들어도 아무도 안 막았고,
그러면 페이지 추가가 다시 두 곳 수정이 되어 이 파일이 있는 이유가 사라진다.

세로 내비로 바꾸면서 목록이 커졌으므로(묶음 5개) 여기서 잠근다.
"""

import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
_PAGES_TS = _SRC / "config" / "pages.ts"
_SIDEBAR = _SRC / "components" / "Sidebar.tsx"


def _pages_src() -> str:
    return _PAGES_TS.read_text()


def _entries() -> list[dict]:
    """`pages` 배열의 항목들 — `{ path: '/x', label: '…', … }` 를 얕게 읽는다."""
    src = _pages_src()
    body = src.split("export const pages", 1)[1]
    body = body.split("\nexport const navPages", 1)[0]
    out = []
    for raw in re.findall(r"\{[^{}]*\}", body):
        entry = {}
        for key, val in re.findall(r"(\w+):\s*'([^']*)'", raw):
            entry[key] = val
        for key in re.findall(r"(\w+):\s*true", raw):
            entry[key] = True
        if "path" in entry:
            out.append(entry)
    return out


def test_the_page_list_is_not_empty():
    """파서가 조용히 0개를 돌려주면 아래 테스트가 전부 무의미해진다."""
    assert len(_entries()) >= 10, "pages.ts 를 못 읽었다 — 아래 검사가 다 헛돈다"


def test_the_sidebar_does_not_keep_its_own_list():
    """**사이드바는 그리는 법만 갖는다.** 무엇을 그릴지는 `pages.ts` 가 정한다.

    경로가 사이드바에 직접 적히기 시작하면 페이지 추가가 두 곳 수정이 되고,
    한쪽만 고치는 사고가 바로 이 파일을 만들게 된 원인이다.
    """
    src = _SIDEBAR.read_text()
    # 라우트처럼 생긴 문자열 리터럴 — `to={page.path}` 같은 파생은 안 걸린다
    hardcoded = re.findall(r"""['"](/[a-z][a-z0-9-]*)['"]""", src)
    assert not hardcoded, f"사이드바가 경로를 직접 들고 있다: {hardcoded}"
    assert "pages" in src, "pages.ts 에서 목록을 안 받아온다"


def test_every_nav_page_has_an_icon():
    """접으면 **아이콘만 남는다** — 없으면 그 항목은 점 하나로 보인다."""
    missing = [e["label"] for e in _entries() if e.get("nav") and not e.get("icon")]
    assert not missing, f"아이콘이 없는 내비 항목: {missing}"


def test_grouped_pages_use_a_declared_group():
    """오타 난 묶음 이름은 **조용히 사라진다** — `navGroups` 가 이름으로 거르므로
    그 페이지만 사이드바에서 안 보이고 아무 에러도 안 난다."""
    declared = set(re.findall(r"'([^']+)'", _pages_src().split("PAGE_GROUPS = [", 1)[1]
                              .split("]", 1)[0]))
    assert declared, "PAGE_GROUPS 를 못 읽었다"
    bad = [(e["label"], e["group"]) for e in _entries()
           if e.get("group") and e["group"] not in declared]
    assert not bad, f"선언 안 된 묶음: {bad}"


@pytest.mark.parametrize("label", ["모델", "데이터셋"])
def test_pages_pushed_off_the_top_bar_are_back(label):
    """**회귀** — 상단 가로 내비에 자리가 없어 이 둘은 `card` 만 달고 `nav` 가
    없었다. 대시보드 카드로만 갈 수 있었다는 뜻이고, 그게 세로 내비로 바꾼 이유다.

    자리가 생겼으니 돌아와 있어야 한다.
    """
    entry = next((e for e in _entries() if e.get("label") == label), None)
    assert entry, f"{label} 페이지가 없다"
    assert entry.get("nav"), f"{label} 이 내비에 없다 — 카드로만 갈 수 있다"


def test_the_estop_button_is_not_moved_into_the_bars():
    """E-stop 은 어느 페이지든 **같은 자리**여야 한다 — 안전 장치의 요건이다.

    상태바(좁다)나 사이드바(접힌다)로 옮기면 급할 때 크기·위치가 달라진다.
    """
    layout = (_SRC / "components" / "Layout.tsx").read_text()
    assert "EStopButton" in layout, "E-stop 이 Layout 밖으로 나갔다"
    for f in ("StatusBar.tsx", "Sidebar.tsx"):
        assert "EStop" not in (_SRC / "components" / f).read_text(), \
            f"{f} 안에 E-stop 이 들어갔다"


def test_the_status_bar_does_not_compose_its_own_activity_names():
    """활동 이름은 백엔드 `LABELS` 를 그대로 쓴다.

    화면이 자기 사전을 들면 같은 활동이 버튼 옆에서는 "정책 서버"인데 상태바에서는
    "정책서버"가 된다 — `DeviceAlerts` 가 문구를 백엔드에서 받는 것과 같은 이유다.
    """
    src = (_SRC / "components" / "StatusBar.tsx").read_text()
    assert "labelOf" in src, "백엔드 라벨을 안 쓴다"
    # 한글 활동 이름을 직접 적어두면 그게 곧 두 번째 사전이다
    for word in ("'추론'", '"추론"', "'학습'", '"학습"', "'녹화'", '"녹화"'):
        assert word not in src, f"상태바가 활동 이름을 직접 적고 있다: {word}"


def test_the_status_bar_stays_read_only():
    """좁은 줄의 작은 버튼은 오조작이 나고, 정작 눌러야 할 때는 큰 버튼이 필요하다.

    상태바는 보여주기만 한다 — 시작·정지는 각 페이지와 E-stop 이 맡는다.
    """
    src = (_SRC / "components" / "StatusBar.tsx").read_text()
    assert "api.post" not in src, "상태바가 명령을 보낸다"
    assert "<button" not in src, "상태바에 버튼이 생겼다 — 읽기 전용이어야 한다"
