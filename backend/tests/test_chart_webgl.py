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
    """이 화면이 그래프를 가장 많이 띄운다 — 집계 3개 + 관절 축 수만큼.

    목록을 만들어 한 자리에서 그리도록 바뀌었으므로 `<PlotlyChart` 는 하나뿐이다.
    개수는 `charts` 배열이 정한다.
    """
    src = _EPISODES.read_text()
    assert "signals.joints.names.forEach" in src, "관절 그래프가 사라졌다"
    assert "const charts" in src, "그래프 목록이 없다"


# ── 그래프 목록과 그리는 순서 ───────────────────────────────────────────────

def test_the_joint_speed_chart_is_gone_but_the_signal_stays():
    """⚠ 그래프만 뺐다. **신호는 못 뺀다** — FSM 의 정지·이동·감속 판정이 전부
    그 값을 쓴다 (`still_speed`/`moving_speed`/`align_speed`).

    말단 속도로 갈아타는 것은 배율 조정이 아니라 재튜닝이다: 실측에서 같은 관절
    속도가 말단에서는 3~6배로 흩어진다 (관절 20 → 말단 33~107mm/s). 어느 관절이
    움직였느냐가 다르기 때문이고, 그게 애초에 말단 신호를 만든 이유다.
    """
    page = _EPISODES.read_text()
    # 목록을 만드는 자리에 관절 속도가 없어야 한다 (아래 FSM 주석에는 나온다)
    body = page.split("const charts = useMemo", 1)[1].split("}, [signals, showJoints])", 1)[0]
    assert "관절 속도" not in body, "그래프가 아직 있다"

    fsm = (Path(__file__).resolve().parents[2] / "phase" / "piper_phase" / "fsm.py").read_text()
    for thr in ("still_speed", "moving_speed", "align_speed"):
        assert f"p.{thr}" in fsm, f"{thr} 판정이 사라졌다"
    assert "speed=speed" in fsm, "신호 자체가 사라졌다"


def test_the_reveal_cannot_stall_on_a_missing_callback():
    """⚠ **회귀** — 앞 그래프가 "다 그렸다" 고 알려줄 때만 다음을 붙였더니,
    그 알림이 한 번이라도 안 오면 **그 아래가 통째로 멈췄다.** 순서를 지키려다
    아예 안 그려지는 경우를 만든 셈이다.

    시계가 민다. 순서는 그대로고, 무엇이 실패하든 다음은 붙는다.
    """
    page = _EPISODES.read_text()
    body = page.split("if (drawnUpTo >= charts.length) return", 1)[1].split("}, [", 1)[0]
    assert "setTimeout" in body, "타이머가 없다 — 콜백에만 기댄다"


def test_the_chart_list_is_built_once():
    """개수를 따로 세면 목록과 어긋나 타이머가 일찍 멈추고 아래가 안 붙는다."""
    page = _EPISODES.read_text()
    assert "charts.length" in page, "목록 길이를 안 쓴다"
    assert "chartCount" not in page, "개수를 따로 센다"


def test_charts_are_added_top_to_bottom():
    """⚠ 전부 한 번에 붙이면 Plotly 가 비동기로 그려서 **아래 것이 먼저**
    나타나는 등 순서가 뒤죽박죽이 된다.

    각 그래프가 다 그려졌다고 알리면 그때 다음 것을 붙인다 — 한꺼번에 열몇 개를
    만드는 부담도 같이 사라진다.
    """
    page = _EPISODES.read_text()
    assert "drawnUpTo" in page, "순서 상태가 없다"
    assert "i <= drawnUpTo" in page, "순서대로 안 붙인다"
    assert "onReady={() => setDrawnUpTo" in page, "다 그렸다는 신호를 안 받는다"

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components"
           / "PlotlyChart.tsx").read_text()
    assert "onInitialized={onReady}" in src, "그래프가 완료를 안 알린다"


def test_switching_episode_restarts_the_order():
    """에피소드를 바꾸면 다시 위에서부터 — 안 그러면 두 번째부터는 한꺼번에 뜬다."""
    page = _EPISODES.read_text()
    body = page.split("const selectEpisode", 1)[1].split("}, [askConfirm])", 1)[0]
    assert "setDrawnUpTo(0)" in body
