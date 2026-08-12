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
