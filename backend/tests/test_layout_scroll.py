"""앱 셸의 스크롤은 `main` 하나다 — 문서(`html`)는 스크롤되지 않는다.

실측(2026-10-15, 개발 서버): `body` 의 세 번째 자식으로 클래스·id 없는 24px absolute `div`(SVG 아이콘 —
브라우저 확장 프로그램이 주입한 것)가 문서 좌표 y=2284 에 붙어, 문서가 뷰포트의 2배가 되고 스크롤바가
하나 더 생겼다. 다른 페이지에서도 생겼다(주입 요소는 페이지와 무관하다).

앱 코드에는 그 요소를 만드는 곳이 없으므로 앱 쪽에서는 **문서 스크롤을 막아** 둔다. 단 `Layout` 안에서만 —
조종 창(`TeleopWindowPage`)은 `Layout` 밖에 살며 문서 스크롤에 기댄다.
"""

from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def test_the_shell_marks_itself_and_the_guard_keys_on_that_mark():
    layout = (_SRC / "components" / "Layout.tsx").read_text()
    css = (_SRC / "index.css").read_text()
    assert "data-app-shell" in layout, "셸 표지가 없어 가드가 걸릴 곳이 없다"
    assert "html:has([data-app-shell])" in css
    rule = css.split("html:has([data-app-shell])", 1)[1].split("}", 1)[0]
    assert "overflow: hidden" in rule


def test_the_guard_is_not_global():
    """⚠ `html`/`body` 전체에 걸면 `Layout` 밖의 조종 창이 문서 스크롤을 잃는다."""
    css = (_SRC / "index.css").read_text()
    # 주석을 빼고 규칙만 본다
    rules = "".join(part.split("/*", 1)[0] for part in css.split("*/"))
    for bare in ("html {", "html{", "body {", "body{", ":root {", ":root{"):
        assert bare not in rules, f"전역 규칙 `{bare}` 가 있다 — 셸이 있을 때만 걸어야 한다"
