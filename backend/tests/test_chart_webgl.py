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


def test_every_chart_slot_reserves_its_height():
    """⚠ **세 번 고치고 나서야 자리를 잡는 게 답이었다.**

    Plotly 는 비동기로 그려서 그 전까지 높이가 0 이다. 열 장이 제각각 채워지면
    그때마다 아래가 밀려 화면이 위아래로 수십 번 튄다 — 신고 그대로다.

    그 사이 시도한 것들은 전부 이 문제를 못 풀었다:
      · 순서대로 하나씩 붙이기  → 붙을 때마다 아래가 밀리는 건 그대로였고,
                                 앞 그래프의 완료 알림이 안 오면 그 아래가 안 그려졌다
      · 알림 대신 타이머        → 멈추지는 않지만 밀림은 그대로

    최종 높이를 미리 주면 **무엇이 언제 그려지든 아무것도 안 움직인다.**
    """
    page = _EPISODES.read_text()
    assert "minHeight: c.extra ? 140 : 160" in page, "자리를 미리 안 잡는다"


def test_the_staggered_reveal_is_gone():
    """순서대로 붙이는 방식이 밀림과 미렌더의 원인이었다 — 되살리면 안 된다."""
    page = _EPISODES.read_text()
    assert "drawnUpTo" not in page, "진행형 렌더가 남아 있다"
    assert "onReady=" not in page, "완료 알림에 다시 기댄다"


def test_the_chart_list_is_built_once():
    """개수를 따로 세면 목록과 어긋난다 — 한 곳에서만 만든다."""
    page = _EPISODES.read_text()
    assert page.count("const charts = useMemo") == 1
    assert "chartCount" not in page, "개수를 따로 센다"
