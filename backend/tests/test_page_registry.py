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


def test_no_page_is_stranded_when_it_leaves_the_nav():
    """⚠ **회귀** — 예전에 `모델`·`데이터셋` 이 `card` 만 달고 `nav` 가 없어
    **대시보드 카드가 유일한 입구**였다. 그 카드를 걷어내면 갈 길이 없어진다.

    지금 `모델` 은 다시 `nav` 에서 빠졌지만 **끊기지 않았다** — `저장소` 화면이
    `ModelsPage` 를 그대로 품고 있고 그 화면은 `nav` 에 있다. 메뉴에 같은 화면이
    두 번 있을 이유가 없어서 뺀 것이다.

    지키려는 것은 "nav 에 있어야 한다" 가 아니라 **"갈 길이 있어야 한다"** 다.
    """
    hub = (_SRC / "pages" / "HubPage.tsx").read_text()
    for page in ("ModelsPage", "DatasetsPage"):
        assert page in hub, f"{page} 가 저장소 화면에서 빠졌다"

    entry = next(e for e in _entries() if e.get("label") == "저장소")
    assert entry.get("nav"), "저장소가 내비에 없다 — 모델·데이터셋이 통째로 고립된다"


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


def test_the_status_bar_does_not_poll_the_disk_walk():
    """`check_disk_usage()` 는 데이터셋·모델 디렉토리를 **통째로 훑는다.**

    장치 요약과 같은 5초 주기에 태우면 그 폴링 자체가 부하가 된다. 디스크는
    녹화·업로드가 끝났을 때만 다시 읽는다 — 그게 디스크를 실제로 움직이는 일이다.
    """
    src = (_SRC / "components" / "StatusBar.tsx").read_text()
    assert "disk-usage" in src, "디스크를 아예 안 읽는다"
    # `setInterval` 로 도는 것은 장치 요약(별도 훅)뿐이어야 한다
    assert "setInterval" not in src, "상태바가 자체 타이머를 돌린다 — 디스크가 딸려간다"
    assert "record_state" in src, "녹화가 끝나도 디스크 표시가 안 바뀐다"


# ── 깊이 범위 슬라이더 (카메라 설정) ──────────────────────────────────────

def _cameras_page() -> str:
    return (_SRC / "pages" / "CamerasPage.tsx").read_text()


def test_depth_range_commits_on_release_not_on_every_tick():
    """`type=range` 는 **끄는 내내** `onChange` 를 쏜다.

    깊이 범위 한 번은 장치 RPC 고 배타 가드(`require_idle`)까지 탄다 — 틱마다
    보내면 슬라이더 한 번 끄는 데 수십 번이 나간다. 놓을 때 한 번만 보낸다.
    """
    src = _cameras_page()
    assert src.count("onCommit={(v) => setDepthRange") == 2, "놓을 때 보내지 않는다"
    # **한 짝이라도** 드래그로 서버를 부르면 그 슬라이더가 홍수를 낸다.
    # 개수로 본다 — 한쪽만 고쳐도 다른 쪽 문구가 검사를 통과시키기 때문이다.
    assert src.count("onChange={(v) => setDepthDraft") == 2, \
        "드래그가 화면 값이 아니라 서버 호출을 움직인다"


def test_depth_sliders_cannot_be_dragged_past_each_other():
    """`far > near` 는 백엔드 규칙이다 (`depth.py` 가 `ValueError` 를 던진다).

    끌어서 넘길 수 있게 두면 **놓는 순간 거부**당한다 — 막을 수 있는 실수를
    굳이 하게 하고, 그 사이 화면은 잘못된 값을 보여준다.
    """
    src = _cameras_page()
    assert "max={Math.max(0, depthDraft.far_mm - DEPTH_STEP)}" in src, \
        "가까운 쪽 손잡이가 먼 쪽을 넘어갈 수 있다"
    assert "min={depthDraft.near_mm + DEPTH_STEP}" in src, \
        "먼 쪽 손잡이가 가까운 쪽을 밑돌 수 있다"


def test_a_rejected_depth_change_snaps_back_to_the_server_value():
    """거부됐는데 끌던 값이 남으면 **화면은 바뀐 척하고 장치는 안 바뀐다.**

    깊이 범위는 녹화한 데이터의 픽셀값 해석이 걸린 값이라 그 거짓말이 특히 비싸다.
    """
    src = _cameras_page()
    body = src.split("const setDepthRange", 1)[1].split("\n  }", 1)[0]
    assert "setDepthDraft(server)" in body, "거부돼도 끌던 값이 남는다"


def test_the_slider_default_matches_the_daemon_default():
    """화면 기본값이 rsd 와 다르면 **아무도 안 바꾼 카메라의 값을 잘못 말한다.**"""
    import re

    front = _cameras_page().split("const DEPTH_DEFAULT = ", 1)[1].split("\n", 1)[0]
    near = int(re.search(r"near_mm:\s*(\d+)", front).group(1))
    far = int(re.search(r"far_mm:\s*(\d+)", front).group(1))

    depth_py = (Path(__file__).resolve().parents[2] / "rs" / "piper_rs" / "depth.py").read_text()
    assert f"near_mm: int = {near}" in depth_py, f"rsd 기본 near 와 다르다 ({near})"
    assert f"far_mm: int = {far}" in depth_py, f"rsd 기본 far 와 다르다 ({far})"


def test_param_slider_still_streams_when_no_commit_is_given():
    """추론 파라미터는 **끄는 동안 팔이 따라오는 게 요점**이다.

    `onCommit` 을 추가하면서 그쪽 동작이 바뀌면 안 된다 — 선택형이어야 한다.
    """
    src = (_SRC / "components" / "ParamSlider.tsx").read_text()
    assert "onCommit?:" in src, "필수 prop 이 되면 기존 호출부가 전부 깨진다"
    assert "onCommit?.(value)" in src, "없을 때를 안 봐준다"


def test_the_hf_token_name_is_labelled_as_a_token():
    """⚠ **회귀** — `wego-hansu (yeonsei_02)` 를 보고 "저게 뭐냐"는 질문이 나왔다.

    괄호 안의 토큰 이름은 데이터셋이나 조직명처럼 읽힌다. 계정명 옆에 그냥
    붙이지 않고 **무엇인지 밝혀서** 적는다.
    """
    # ⚠ **대시보드에서 설정으로 옮겼다.** 대시보드는 "지금 괜찮은가" 에만 답하는
    #   자리이고 로그인은 상태가 아니라 설정이다. 옮기면서 이 수정이 사라지지
    #   않도록 검사도 따라온다.
    # ⚠ 자리가 두 번 옮겨졌다: 대시보드 배지 → 설정 배지 → **설정의 저장소 탭
    #   패널**. 옮길 때마다 이 수정이 따라와야 한다 — 원래 문구가 아니라 그
    #   **이유**(정체가 안 보인다)가 지켜야 할 것이다.
    src = (_SRC / "components" / "HfAccountPanel.tsx").read_text()
    assert "토큰 {acc.token_name}" in src, "토큰이라고 안 밝힌다"
    assert "` (${acc.token_name})`" not in src, "괄호로만 붙여 정체가 안 보인다"
    settings = (_SRC / "pages" / "SettingsPage.tsx").read_text()
    assert "HfAccountPanel" in settings, "옮겼는데 아무 데도 안 쓰인다"


def test_the_repository_header_is_one_row():
    """⚠ 제목·탭 / 로컬·Hub·새로고침 / 디스크 상자가 **세 줄**로 쌓여 있었다 —
    정작 봐야 할 목록이 그만큼 아래로 밀렸다. 셋 다 "무엇을 어디서 보나" 를
    정하는 것이라 한 줄에 있어도 읽힌다."""
    hub = (_SRC / "pages" / "HubPage.tsx").read_text()
    header = hub.split("<h1", 1)[0][-400:] + hub.split("<h1", 1)[1].split("</div>\n\n", 1)[0]
    assert "flex flex-wrap items-center" in header, "머리줄이 한 줄로 안 붙는다"
    assert "DiskUsageBar compact" in hub, "디스크가 아직 상자다"


def test_the_source_choice_is_shared_between_tabs():
    """⚠ `로컬|Hub` 는 모델·데이터셋 **둘이 공유하는 선택**이다. 각 페이지가 따로
    들고 있으면 탭을 옮길 때마다 `로컬` 로 되돌아간다."""
    hub = (_SRC / "pages" / "HubPage.tsx").read_text()
    assert "tab={source}" in hub, "출처를 자식에게 안 넘긴다"
    for page in ("ModelsPage.tsx", "DatasetsPage.tsx"):
        src = (_SRC / "pages" / page).read_text()
        assert "const tab = tabProp ?? ownTab" in src, f"{page} 가 부모 선택을 무시한다"


def test_refresh_does_not_look_like_a_third_choice():
    """⚠ `로컬|Hub|새로고침` 이 한 묶음에 같은 모양으로 있었다. `로컬`·`Hub` 는
    **고르는 것**이고 `새로고침` 은 **하는 것**인데, 같이 두면 세 번째 선택지처럼
    읽힌다 — 누르면 `Hub` 가 꺼지는 줄 안다.

    선택은 묶음 안의 켜진 칸으로, 동작은 늘 테두리 있는 버튼으로 둔다.
    """
    hub = (_SRC / "pages" / "HubPage.tsx").read_text()
    picker = hub.split("setSource('hub')", 1)[1].split("setRefreshKey", 1)[0]
    assert "</div>" in picker, "새로고침이 아직 선택 묶음 안에 있다"
    assert "w-px bg-neutral-700" in picker, "칸막이가 없다"
    action = hub.split("setRefreshKey((n) => n + 1)", 1)[1][:200]
    assert "className={action}" in action, "동작 버튼 모양이 아니다"
    assert "border border-neutral-600" in hub, "테두리가 항상 있지 않다"
