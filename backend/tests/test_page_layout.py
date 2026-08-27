"""녹화·학습 페이지에서 로그와 CLI 가 가로를 다 쓰는지.

예전에는 두 페이지 모두 `lg:grid-cols-[2fr_1fr]` 로 좌우를 갈라 **로그를 좁은
우측 열에 넣었다.** LeRobot 로그는 경로와 JSON 이 길어서 한 줄이 계속 접혔고,
CLI 명령어도 4줄로 접혀 읽기 어려웠다.

여기서 잠그는 것: **로그와 CLI 가 좌우 분할 안에 들어가 있지 않을 것.**
"""

import re
from pathlib import Path

import pytest

_PAGES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"


def _blocks(src: str) -> list[str]:
    """`grid-cols-[...fr_...fr]` 로 좌우를 가르는 컨테이너의 여는 태그들."""
    return re.findall(r'className="[^"]*lg:grid-cols-\[[^"]*\]"', src)


@pytest.mark.parametrize("page", ["RecordingPage", "TrainingPage"])
def test_log_is_not_inside_a_narrow_column(page):
    """로그가 좌우 분할 컨테이너 **안**에 있으면 안 된다.

    분할 컨테이너가 열린 뒤 닫히기 전에 `<LogViewer` 가 나오면 좁은 열에 갇힌 것이다.
    들여쓰기로 판단한다 — 로그 블록은 분할 컨테이너와 같은 깊이여야 한다.
    """
    src = (_PAGES / f"{page}.tsx").read_text()
    for line in src.splitlines():
        if "<LogViewer" in line:
            indent = len(line) - len(line.lstrip())
            # 분할 컨테이너 안이면 최소 두 단계는 더 들어간다
            assert indent <= 14, f"{page}: 로그가 너무 깊다(열 안?) — {line.strip()!r}"


@pytest.mark.parametrize("page", ["RecordingPage", "TrainingPage"])
def test_settings_use_two_columns_not_a_narrow_strip(page):
    """설정은 2열로 담는다 — 로그를 빼고 전체폭이 되면 입력창이 과하게 넓어진다."""
    src = (_PAGES / f"{page}.tsx").read_text()
    assert "lg:grid-cols-2" in src, f"{page}: 설정을 2열로 담지 않는다"


def test_recording_keeps_a_wide_preview_while_running():
    """녹화 중에는 미리보기가 주인공이라 좌우 분할이 남아 있어야 한다."""
    src = (_PAGES / "RecordingPage.tsx").read_text()
    assert "lg:grid-cols-[2fr_1fr]" in src, "녹화 중 미리보기 우선 배치가 사라졌다"


# ── 배치 토글 (학습·비전) ────────────────────────────────────────────────────

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def test_the_layout_toggle_is_one_component():
    """같은 토글이 두 페이지에 필요해졌다. 복사하면 저장 키 규칙이나 라벨이
    한쪽만 바뀐다 — 이 저장소에서 반복해서 난 갈라짐이다(관절 순서, 페이지 목록)."""
    assert (_SRC / "components" / "LayoutToggle.tsx").exists()
    for page in ("TrainingPage.tsx", "VisionPage.tsx"):
        src = (_SRC / "pages" / page).read_text()
        assert "LayoutToggle" in src, f"{page} 가 공용 토글을 안 쓴다"
        assert "localStorage.getItem('vision-layout')" not in src, \
            f"{page} 에 저장 키가 직접 박혀 있다"


def test_training_can_switch_layout_while_running():
    """학습 중에 보고 싶은 것이 그래프일 때도 로그일 때도 있다 —
    화면 크기와 그때 사정이 정한다."""
    src = (_SRC / "pages" / "TrainingPage.tsx").read_text()
    running = src.split("      ) : (", 1)[1]
    assert "<LayoutToggle" in running, "실행 중 화면에 토글이 없다"
    assert "layout === 'row'" in running, "배치가 안 바뀐다"


def test_the_layout_choice_is_remembered():
    """매번 다시 고르게 하면 성가시다 — 배치는 취향과 화면 크기가 정한다."""
    src = (_SRC / "components" / "LayoutToggle.tsx").read_text()
    assert "localStorage.setItem" in src and "localStorage.getItem" in src


def test_the_running_log_sits_inside_the_switchable_layout():
    """⚠ 학습 **중** 로그는 좌우 분할 안에 있어도 된다 — 그게 가로 배치의 요점이다.

    이 파일 위쪽 규칙("로그를 좁은 열에 넣지 말 것")과 헷갈리기 쉬운데, 다르다:
    그쪽은 **고정** 2:1 분할이라 로그가 늘 좁았고, 여기는 사용자가 언제든 세로로
    돌릴 수 있다. 좁으면 세로로 바꾸면 된다.
    """
    src = (_SRC / "pages" / "TrainingPage.tsx").read_text()
    running = src.split("      ) : (", 1)[1]
    layout_open = running.index("layout === 'row'")
    assert running.index("LogViewer", layout_open) > layout_open, \
        "로그가 배치 컨테이너 밖에 있다 — 가로로 바꿔도 안 옮겨간다"


# ── 녹화 미리보기 ────────────────────────────────────────────────────────────

def test_every_recording_camera_is_visible_at_once():
    """⚠ **회귀** — 2열 고정 격자라 카메라가 셋 이상이면 둘째 줄로 접혔다.

    녹화 중 미리보기의 용도는 *지금 모든 카메라가 제대로 찍고 있나* 하나다.
    한 대라도 아래로 접히면(스크롤해야 보이면) 그 확인이 안 된다 —
    가려진 쪽이 하필 빠진 카메라다.
    """
    from conftest import code_only

    src = (_SRC / "components" / "RecordPreview.tsx").read_text()
    body = code_only(src)
    assert "grid-cols-2" not in body, "미리보기가 다시 2열로 접힌다"
    assert "flex" in body, "한 줄로 안 세운다"


def test_the_preview_row_scrolls_instead_of_shrinking_to_nothing():
    """한 줄이 되면 카메라가 늘수록 폭이 줄어든다. 바닥을 안 정하면
    여섯 대쯤에서 **무엇이 찍혔는지 알아볼 수 없는 띠**가 된다.

    최소 폭을 두고, 넘치면 옆으로 밀어서 본다.
    """
    from conftest import code_only

    src = code_only((_SRC / "components" / "RecordPreview.tsx").read_text())
    assert "min-w-[" in src, "최소 폭이 없어 무한정 납작해진다"
    assert "overflow-x-auto" in src, "최소 폭을 넘으면 잘려서 안 보인다"


def test_the_episode_viewer_can_switch_layout():
    """사진과 그래프 중 무엇을 넓게 볼지는 그때 사정이 정한다 —
    프레임을 뜯어볼 때와 신호를 훑을 때가 다르다."""
    src = (_SRC / "pages" / "EpisodesPage.tsx").read_text()
    assert "LayoutToggle" in src, "공용 토글을 안 쓴다"
    assert "useLayout('episodes')" in src, "저장 키가 페이지별로 안 갈린다"
    assert "layout === 'row'" in src, "배치가 안 바뀐다"


def test_the_episode_list_is_not_part_of_the_switch():
    """⚠ 목록은 늘 옆에 붙어 있어야 한다 — J/K 로 오가며 비교하는 화면이다.

    바뀌는 것은 **사진과 그래프의 관계**뿐이다.
    """
    src = (_SRC / "pages" / "EpisodesPage.tsx").read_text()
    aside = src.split("<aside", 1)[1].split("</aside>", 1)[0]
    assert "layout" not in aside, "목록이 배치 선택에 딸려간다"


def test_the_charts_and_the_video_end_up_in_different_columns():
    """가로 배치의 요점이다 — 둘이 같은 칸에 들어가면 넓어지지 않는다."""
    src = (_SRC / "pages" / "EpisodesPage.tsx").read_text()
    body = src.split("layout === 'row'", 1)[1]
    cam = body.index("{/* 카메라")
    chart = body.index("{/* 신호 그래프")
    between = body[cam:chart]
    assert between.count("<div className=\"space-y-3\">") >= 1 or "</div>" in between
    assert chart > cam, "그래프가 사진보다 앞에 있다"


def test_the_cameras_always_stack():
    """⚠ 두 번 틀렸던 자리다.

    처음엔 배치와 무관하게 늘 나란히였고, 다음엔 `layout` 상태만 보게 했는데
    두 칸이 되는지는 브레이크포인트가 정하므로 좁은 화면에서 어긋났다.

    실제로는 **어느 배치에서도 쌓는 게 맞다.** 가로 배치의 사진 칸은 세로로 긴데,
    거기에 나란히 두면 폭을 반씩 나눠 갖고 아래가 통째로 빈다.
    """
    src = (_SRC / "pages" / "EpisodesPage.tsx").read_text()
    cam = src.split("{/* 카메라 —", 1)[1][:700]
    assert 'className="flex flex-col gap-3"' in cam, "세로 쌓기가 아니다"
    assert "layout ===" not in cam, "배치 토글에 다시 묶였다"


def test_everything_indexed_by_frame_shares_a_column():
    """⚠ 진행바·페이즈 트랙·신호 그래프는 **같은 x축(프레임)** 을 쓴다.

    사진 쪽에 남겨두면 가로 배치에서 재생헤드가 두 칸에 흩어진다.
    """
    src = (_SRC / "pages" / "EpisodesPage.tsx").read_text()
    right = src.split("프레임으로 색인되는 것은", 1)[1]
    for marker in ('type="range"', "페이즈 트랙", "신호 그래프"):
        assert marker in right, f"{marker} 가 시간축 칸에 없다"
    left = src.split("프레임으로 색인되는 것은", 1)[0]
    assert 'type="range"' not in left, "진행바가 사진 쪽에 남아 있다"
