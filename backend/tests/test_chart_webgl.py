"""그래프가 WebGL 컨텍스트를 낭비하지 않는지.

`scattergl` 은 그래프마다 WebGL 컨텍스트를 하나씩 잡는다. 브라우저 한도(크롬
기준 십수 개)를 넘으면 **오래된 것부터 버려지고, 버려진 그래프는 하얗게 남는다.**
에러도 경고도 없다.

에피소드 화면에서 관절 그래프(7축)를 펼치자 위쪽 속도 그래프들이 그렇게 비었다.
"""

import re
from pathlib import Path

_CHART = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "PlotlyChart.tsx"
_EPISODES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "EpisodesPage.tsx"


def test_the_trace_type_depends_on_how_many_points_there_are():
    """⚠ **회귀** — `scattergl` 이 무조건이었다.

    점이 많을 때만 GL 이 값을 한다. 이 화면의 신호는 에피소드당 수백 점이라
    SVG 로 충분하고, 그러면 컨텍스트를 아예 안 쓴다.
    """
    src = _CHART.read_text()
    assert "GL_MIN_POINTS" in src, "점 수를 안 본다"
    body = src.split("const kind = useMemo", 1)[1].split("\n\n", 1)[0]
    assert "'scattergl'" in body and "'scatter'" in body, "둘 중 하나만 쓴다"
    assert "type: kind" in src, "고른 값을 안 쓴다"


def test_no_chart_hardcodes_the_gl_trace():
    """한 곳이라도 무조건 GL 이면 그 그래프가 남의 컨텍스트를 뺏는다."""
    src = _CHART.read_text()
    # 주석과 판정식 밖에서 `type: 'scattergl'` 이 나오면 안 된다
    code = re.sub(r"//.*$", "", src, flags=re.M)
    assert "type: 'scattergl'" not in code


def test_the_threshold_is_far_above_a_typical_episode():
    """실측: 311프레임 에피소드. 문턱이 그보다 낮으면 평소에도 GL 을 쓴다."""
    src = _CHART.read_text()
    n = int(re.search(r"GL_MIN_POINTS\s*=\s*(\d+)", src).group(1))
    assert n >= 2000, f"문턱이 너무 낮다: {n}"


def test_the_episode_screen_can_show_many_charts_at_once():
    """이 화면이 컨텍스트를 가장 많이 쓴다 — 집계 3개 + 관절 축 수만큼."""
    src = _EPISODES.read_text()
    assert "signals.joints.names.map" in src, "관절 그래프가 사라졌다"
    assert src.count("<PlotlyChart") >= 4
