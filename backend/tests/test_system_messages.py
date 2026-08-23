"""시스템 메시지 — 어느 페이지에서든 같은 자리에 뜬다 (components/SystemMessages.tsx).

증상 둘이 겹쳤다:

1. 장치 경보가 `Layout` 안 **문서 흐름**에 있어 수집 페이지처럼 긴 화면에서는
   스크롤을 올려야 보였다 — 정작 필요한 순간에 안 보였다
2. 나머지 실패는 `window.alert` 17곳으로 흩어져 있었다

2번은 취향 문제가 아니다. `alert`/`confirm` 은 **JS 이벤트 루프를 멈추고**,
그러면 E-stop heartbeat(500ms)도 멈춰 2초 타임아웃에 추론이 강제 종료된다 —
이 저장소가 `window.confirm` 으로 실제로 겪은 사고다.
"""

import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
_PAGES = sorted((_SRC / "pages").glob("*.tsx"))


def _code(path: Path) -> str:
    """주석을 걷은 소스. 설명문에 `alert` 이 나오는 건 설명이다."""
    return re.sub(r"\{?/\*[\s\S]*?\*/\}?|//.*", "", path.read_text())


@pytest.mark.parametrize("path", _PAGES, ids=lambda p: p.stem)
def test_no_blocking_dialogs(path):
    """**회귀** — `alert`/`confirm` 은 heartbeat 를 막아 팔을 세운다."""
    code = _code(path)
    for bad in ("window.alert(", "window.confirm(", "window.prompt("):
        assert bad not in code, f"{path.name}: {bad} 이 이벤트 루프를 막는다"
    # 접두사 없는 전역 호출도 같다
    assert not re.search(r"(?<![\w.])alert\s*\(", code), f"{path.name}: 전역 alert()"
    assert not re.search(r"(?<![\w.])confirm\s*\(", code), f"{path.name}: 전역 confirm()"


def test_the_host_is_fixed_not_in_flow():
    """문서 흐름에 두면 긴 페이지에서 스크롤을 올려야 보인다 — 그게 원래 버그다."""
    src = (_SRC / "components" / "SystemMessages.tsx").read_text()
    host = src.split("export function SystemMessageHost", 1)[1]
    assert "fixed" in host, "호스트가 fixed 가 아니다"
    assert "z-50" in host, "다른 요소에 가릴 수 있다"


def test_confirm_is_available_and_non_blocking():
    """삭제·리바인딩은 물어봐야 하는데, `window.confirm` 은 heartbeat 를 막는다.

    그래서 같은 인터페이스가 **Promise 를 돌려주는** 확인을 제공한다.
    """
    src = (_SRC / "components" / "SystemMessages.tsx").read_text()
    assert "Promise<boolean>" in src, "확인이 논블로킹이 아니다"
    assert "aria-modal" in src, "모달로 안 그린다"


def test_the_provider_sits_outside_the_router():
    """페이지를 옮겼다고 경고가 사라지면 안 된다."""
    main = (_SRC / "main.tsx").read_text()
    i_provider = main.index("<SystemMessageProvider>")
    i_router = main.index("<BrowserRouter>")
    assert i_provider < i_router, "프로바이더가 라우터 안에 있다 — 이동 시 메시지가 날아간다"


def test_device_alerts_have_no_ui_of_their_own():
    """장치 경보가 자기 배너를 그리면 메시지가 두 곳에 뜬다."""
    src = _code(_SRC / "components" / "DeviceAlerts.tsx")
    assert "useSystemMessage" in src, "시스템 메시지를 안 쓴다"
    assert "return null" in src, "자기 UI 를 그리고 있다"


def test_pages_do_not_choose_their_own_colours():
    """같은 실패가 화면마다 다르게 보이면 안 된다 — 색·위치는 호스트가 정한다."""
    for path in _PAGES:
        code = _code(path)
        for call in re.findall(r"notify\(\{[^}]*\}", code):
            assert "className" not in call, f"{path.name}: 페이지가 표현을 정한다"


def test_level_is_stated_not_guessed():
    """`notify` 는 심각도를 **받는다** — 문구에서 짐작하지 않는다."""
    src = (_SRC / "components" / "SystemMessages.tsx").read_text()
    assert "level: MessageLevel" in src
    for guess in ("includes('실패')", 'includes("실패")', "startsWith('⚠"):
        assert guess not in src, "문구로 심각도를 추측한다"


def test_a_bare_confirm_call_is_never_the_browser_one():
    """⚠ **`window.confirm` 은 이벤트 루프를 멈춰 E-stop heartbeat 를 끊는다** —
    추론 중이면 2초 뒤 팔이 강제로 선다. 이 저장소가 실제로 겪은 사고다.

    그런데 `confirm(` 은 **전역이 있어서 TypeScript 가 안 잡는다.** 훅에서
    `confirm: askConfirm` 으로 이름을 바꿔 받아놓고 습관대로 `confirm(` 이라고
    쓰면 조용히 브라우저 것이 불린다 — 실제로 그렇게 썼다가 잡았다.

    규칙: 맨 `confirm(`/`alert(` 는 **그 파일이 훅에서 그 이름 그대로 받았을 때만**
    허용한다.
    """
    import re
    from pathlib import Path

    from conftest import code_only

    root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    offenders = []
    for f in list(root.rglob("*.tsx")) + list(root.rglob("*.ts")):
        if f.name == "SystemMessages.tsx":     # 여기가 그 `confirm` 을 정의한다
            continue
        src = code_only(f.read_text())
        # 훅에서 `confirm` 을 그 이름으로 받았나
        destructured = re.search(r"const\s*\{[^}]*\bconfirm\b[^}]*\}\s*=\s*useSystemMessage",
                                 src) and "confirm:" not in src
        for name in ("confirm", "alert"):
            if destructured and name == "confirm":
                continue
            # `window.confirm(` 도 `foo.confirm(` 도 아닌 **맨** 호출
            if re.search(rf"(?<![.\w]){name}\s*\(", src):
                offenders.append(f"{f.relative_to(root)}: {name}(")
    assert not offenders, (
        "브라우저 전역이 불린다 — 이벤트 루프가 멈춰 heartbeat 가 끊긴다: "
        + ", ".join(offenders))
