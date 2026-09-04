def test_the_daemon_takes_a_baseline_when_it_starts():
    """⚠ **CAN 오류 카운터는 커널 쪽이라 데몬을 다시 띄워도 그대로다.** 기동 때
    기준선을 안 잡으면 새로 뜬 데몬이 어제의 1억을 물려받아 보여준다 — 데몬이
    새로 떴다는 것은 "여기서부터 본다" 는 뜻이다."""
    import inspect
    import textwrap

    from piper_robot.bus_watch import BusWatch

    src = python_code_only(textwrap.dedent(inspect.getsource(BusWatch.start)))
    assert "self.rebase()" in src, "기동해도 기준선을 안 잡는다"


def test_rebasing_drops_the_old_samples_too():
    """기준선과 이력은 **같이** 새로 잡혀야 한다 — 따로 놀면 숫자는 초기화됐는데
    그래프만 옛 구간을 보여준다."""
    import inspect
    import textwrap

    from piper_robot.bus_watch import BusWatch

    src = python_code_only(textwrap.dedent(inspect.getsource(BusWatch.rebase)))
    assert "_hist.pop" in src and "_base[name]" in src, "둘 중 하나만 새로 잡는다"


"""CAN 버스 상태 탭 — 지금 상태 + 누적 오류 + 트래픽.

⚠ **카운터만 보여주면 오독한다.** 인터페이스를 다시 열면 0 으로 돌아가므로
절대값끼리 비교하면 안 된다 — 실측: can2·can3 이 1초 차이로 올라왔는데 각각
0 과 34,794 였다. 그래서 트래픽을 같이 내고, 화면이 백만 프레임당으로 환산한다.
"""

from pathlib import Path

import pytest
import textwrap

from conftest import code_only, python_code_only
from fastapi.testclient import TestClient

from app.main import app

PANEL = (Path(__file__).resolve().parents[2] / "frontend" / "src"
         / "components" / "BusStatusPanel.tsx")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_it_reports_state_counters_and_traffic_together(client, monkeypatch):
    """⚠ 셋을 **함께** 내야 한다. 상태만 보면 잠깐 나빠졌다 돌아오는 버스를
    영영 못 잡고, 카운터만 보면 가동 시간이 다른 버스를 잘못 비교한다."""
    from app.services import robot_manager as rm

    rows = [{"iface": "can0", "state": "ERROR-ACTIVE", "healthy": True,
             "bitrate": 1000000, "counters": {"bus_off": 0}, "errors_total": 0,
             "rx_packets": 40979846, "tx_packets": 153,
             "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0}]
    monkeypatch.setattr(rm, "_call", lambda m, *a, **k: rows if m == "bus_status" else None)

    got = client.get("/api/robots/bus")
    assert got.status_code == 200, got.text
    b = got.json()["buses"][0]
    assert b["state"] == "ERROR-ACTIVE"
    assert b["counters"] and b["rx_packets"] and b["errors_total"] == 0


def test_a_dead_daemon_is_said_out_loud(client, monkeypatch):
    """⚠ 버스 상태를 못 읽는 것과 버스가 멀쩡한 것은 다르다 — 빈 목록을 내면
    "문제 없음" 으로 읽힌다."""
    from app.services import robot_manager as rm

    monkeypatch.setattr(rm, "_call", lambda m, *a, **k: None)
    got = client.get("/api/robots/bus")
    assert got.status_code == 503 and "robotd" in got.json()["detail"]


def test_the_host_reads_it_because_the_container_cannot():
    """⚠ 게이트웨이는 컨테이너라 `/sys/class/net` 이 안 보인다 — robotd 에
    물어야 한다. 게이트웨이에서 직접 읽으면 배포에서만 조용히 빈다."""
    import inspect

    from app.routers.robots import bus_status

    # docstring 이 `/sys/class/net` 을 **쓰지 말라고** 적고 있어서, 그대로
    # 뒤지면 그 설명이 검사에 걸린다
    # ⚠ `python_code_only` 는 ast 로 되짚으므로 따옴표가 정규화된다 —
    #   따옴표에 기대는 검사는 조용히 깨진다.
    src = python_code_only(textwrap.dedent(inspect.getsource(bus_status))).replace('"', "'")
    assert "_call('bus_status'" in src, "robotd 에 안 묻는다"
    assert "/sys/class/net" not in src


def test_the_screen_normalizes_by_traffic():
    """⚠ 절대값 비교는 틀린다(0 vs 34,794 가 같은 시각의 두 버스였다).
    백만 프레임당으로 환산하면 가동 시간이 달라도 비교가 된다."""
    src = code_only(PANEL.read_text())
    assert "perMillion" in src, "트래픽으로 정규화하지 않는다"
    assert "34,794" in PANEL.read_text(), "왜 정규화가 필요한지 화면이 안 말한다"


def test_auto_refresh_is_optional_and_off_by_default():
    """⚠ 이 화면은 인터페이스마다 `ip` 를 부르므로 공짜가 아니고, 팔을 만지는
    중에 배경 폴링이 도는 것을 사람이 모르면 안 된다 — 기본은 꺼짐, 켜는 건 선택."""
    src = code_only(PANEL.read_text())
    assert "useState(false)" in src, "자동 새로고침이 기본으로 켜져 있다"
    assert "INTERVALS" in src, "주기를 고를 수 없다"
    assert "새로고침" in PANEL.read_text(), "수동 새로고침 버튼이 없다"


# ── 버스 초기화 ─────────────────────────────────────────────────────────────

def test_reset_does_not_claim_to_zero_the_counters():
    """⚠ **down/up 으로 오류 카운터가 안 지워진다** — gs_usb 실측:
    130,730,379 → 130,730,379. 지운 척하면 사람이 "고쳐졌다" 로 읽는다.
    대신 **기준선**을 잡아 "초기화 이후" 를 센다."""
    import inspect
    import textwrap

    from piper_robot.can import reset_bus

    src = python_code_only(textwrap.dedent(inspect.getsource(reset_bus)))
    assert "restart-ms" not in src, "지원하지 않는 자동 복구를 건다"
    assert "counters" in src, "기준선을 잡을 값을 안 돌려준다"


def test_reset_refuses_while_the_arm_is_in_use(monkeypatch):
    """⚠ 초기화는 연결을 끊는다 — 돌고 있는 작업이 통째로 죽는다."""
    import asyncio

    from fastapi import HTTPException

    from app.routers.robots import BusResetRequest, bus_reset
    from app.services import exclusivity

    monkeypatch.setattr(exclusivity, "running",
                        lambda: [exclusivity.Activity.TELEOP])
    with pytest.raises(HTTPException) as err:
        asyncio.run(bus_reset(BusResetRequest(iface="can0")))
    assert err.value.status_code == 409


def test_the_screen_says_the_counters_survive():
    """⚠ 화면이 "0 으로 초기화됩니다" 라고 하면 거짓말이다 — 실제로는 안 지워진다."""
    src = PANEL.read_text()
    assert "안 지워집니다" in src
    assert "초기화 이후" in src


def test_after_a_reset_the_screen_shows_the_new_count_not_the_old_one():
    """⚠ 누적 카운터는 초기화해도 안 지워진다. 그런데 화면이 계속 누적값을
    주황색으로 보여주면 **"초기화가 소용없다" 로 읽힌다** — 실제로 그렇게
    보고됐다("경고 4 수동 130,673,575 … 이건 안 없어지네").

    기준선이 있으면 **항목별로도** 초기화 이후 값을 보여줘야 한다. 합계만으로는
    항목 칸이 여전히 누적값을 쓸 수밖에 없다."""
    src = code_only(PANEL.read_text())
    assert "b.counters_since_reset ?? b.counters" in src, \
        "초기화 뒤에도 항목별로 누적값을 보여준다"
    assert "누적" in PANEL.read_text(), "누적값을 어디서도 못 본다"


def test_every_derived_value_uses_the_same_baseline():
    """⚠ 기준선을 잡을 거면 **전부** 잡아야 한다. 오류만 기준선을 두고 트래픽은
    누적을 쓰던 때, 백만 프레임당 오류가 `50,796,791` 로 떴다 — 다른 항목은 전부
    0 인데 이것만 옛 숫자였다. 섞인 기준은 틀린 값보다 나쁘다: 그럴듯해 보인다.
    """
    import inspect
    import textwrap

    from piper_robot.bus_watch import BusWatch

    src = python_code_only(textwrap.dedent(inspect.getsource(BusWatch.rebase)))
    for field in ("rx_packets", "tx_packets", "counters"):
        assert field in src, f"기준선에 {field} 가 없다"

    ui = code_only(PANEL.read_text())
    seg = ui.split("function perMillion", 1)[1][:400]
    assert "errors_since_reset" in seg and "rx_since_reset" in seg, \
        "파생값이 누적과 초기화 이후를 섞어 쓴다"


def test_the_charts_plot_rate_not_the_running_total():
    """⚠ 카운터는 단조증가라 누적을 그리면 선이 언제나 우상향이고 아무것도 안
    말한다. 그릴 값은 **초당 증가량**이다."""
    from pathlib import Path

    charts = (Path(__file__).resolve().parents[2] / "frontend" / "src"
              / "components" / "BusCharts.tsx").read_text()
    assert "초당" in charts, "무엇을 그리는지 화면이 안 말한다"

    # 속도 계산은 **데몬**에 있다 — 화면은 받아 그리기만 한다
    from piper_robot.bus_watch import _rate

    assert _rate(300, 100, 2.0) == 100.0
    assert _rate(100, 300, 2.0) == 0.0, "카운터가 되감기면 음수 속도가 나온다"
    assert _rate(None, 100, 2.0) == 0.0


def test_the_daemon_collects_so_the_tab_opens_with_a_graph():
    """⚠ **브라우저가 이력을 모으면 탭을 열 때마다 빈 그래프로 시작한다.** 표본이
    쌓이길 기다려야 하고, 탭을 닫으면 그동안이 사라진다 — 정작 보고 싶은 것은
    "내가 안 보던 동안 무슨 일이 있었나" 인데 그때가 비어 있는 셈이다."""
    from piper_robot.bus_watch import BusWatch, INTERVAL_S

    assert INTERVAL_S == 2.0
    w = BusWatch()
    assert w.history("can0") == []
    w._push("can0", {"t": 1.0, "rx": 10.0, "tx": 0.0, "err": 0.0})
    assert len(w.history("can0")) == 1

    ui = code_only(PANEL.read_text())
    assert "b.history" in ui, "화면이 데몬 이력을 안 쓴다"


def test_a_reset_starts_the_graph_over_too():
    """⚠ 초기화 전 표본은 **다른 기준**의 값이다. 남겨 두면 그래프가 두 기준을 한
    선에 섞어 그리고, 화면의 다른 숫자는 전부 "초기화 이후" 인데 그래프만 옛
    구간을 보여준다."""
    from piper_robot.bus_watch import BusWatch

    import inspect
    import textwrap

    from piper_robot.bus_watch import BusWatch
    from piper_robot.hub import RobotHub

    w = BusWatch()
    w._push("can0", {"t": 1.0, "rx": 1.0, "tx": 0.0, "err": 0.0})
    w.clear("can0")
    assert w.history("can0") == [], "버리라 했는데 옛 표본이 남는다"

    src = python_code_only(textwrap.dedent(inspect.getsource(RobotHub.bus_reset)))
    assert "bus_watch.rebase" in src, "초기화가 그래프 기준을 안 맞춘다"


def test_a_stalled_sampler_does_not_invent_a_quiet_period():
    """⚠ 표본 간격이 크게 벌어졌으면(데몬이 멈췄다 깼다) 그 구간은 버린다 —
    평균이 뭉개져 "그동안 조용했다" 로 보인다."""
    import inspect
    import textwrap

    from piper_robot.bus_watch import BusWatch

    src = python_code_only(textwrap.dedent(inspect.getsource(BusWatch._sample)))
    assert "INTERVAL_S * 5" in src, "간격이 벌어진 구간을 그대로 쓴다"


def test_one_ip_call_per_sample():
    """⚠ 2초마다 네 버스를 재는데 인터페이스마다 `ip` 를 세 번 부르면 초당 여섯
    번의 프로세스 생성이다. 한 출력에 상태·비트레이트·카운터가 다 들어 있다."""
    import inspect
    import textwrap

    from piper_robot.can import bus_stats

    src = python_code_only(textwrap.dedent(inspect.getsource(bus_stats)))
    assert src.count("subprocess.run") == 1, "샘플마다 ip 를 여러 번 부른다"


# ── 축과 창 ─────────────────────────────────────────────────────────────────

def test_the_rate_chart_does_not_force_a_zero_baseline():
    """⚠ 이 그래프의 질문은 "얼마나 많나" 가 아니라 **"변했나"** 다. 초당 3000
    프레임이 ±50 으로 흔들리는데 0 부터 그리면 선이 맨 위에 붙어 **멈춘 것처럼
    보인다** — 실기에서 "왜 그래프가 안 바뀌지" 로 보고됐다(실측 2981~3077).

    잘린 축은 크기 비교를 왜곡하므로 위아래 눈금에 **실제 값을 적는다.**"""
    from pathlib import Path

    charts = (Path(__file__).resolve().parents[2] / "frontend" / "src"
              / "components" / "BusCharts.tsx").read_text()
    src = code_only(charts)
    assert "const lo = Math.min(...vals)" in src, "축을 데이터에 맞추지 않는다"
    assert "{fmt(v)}" in src, "잘린 축인데 값을 안 적는다"


def test_rx_and_tx_are_separate_plots_not_a_dual_axis():
    """⚠ RX 는 초당 수천, TX 는 0 에 가깝다. 한 축에 얹으면 TX 가 바닥에 깔려
    안 보이고, 축을 둘로 쪼개면 이중 축이 된다 — 각자 자기 배율의 **별개 플롯**이
    답이다(소형 다중)."""
    ui = code_only(PANEL.read_text())
    assert ui.count("<RateChart") == 2, "두 계열이 한 플롯에 있다"
    assert 'field="rx"' in ui and 'field="tx"' in ui


def test_the_daemon_always_keeps_the_longest_window():
    """⚠ 화면이 고른 만큼만 모으면, 5분으로 보다가 30분으로 바꾸는 순간 그 25분이
    비어 있다 — 정작 "아까 뭐였지" 를 보려고 바꾸는 것인데. 보관은 싸고, 뒤늦게
    되돌릴 수 없는 쪽은 **안 모은 시간**이다."""
    from piper_robot.bus_watch import HISTORY, INTERVAL_S
    from app.routers.robots import BUS_WINDOWS_MIN

    assert HISTORY * INTERVAL_S >= max(BUS_WINDOWS_MIN) * 60, \
        "가장 긴 창을 데몬이 못 채운다"
    assert BUS_WINDOWS_MIN == (5, 10, 30)


def test_only_the_offered_windows_are_accepted(monkeypatch):
    """⚠ 임의의 분을 받으면 900개를 통째로 내보내는 요청이 열린다."""
    import asyncio

    from fastapi import HTTPException

    from app.routers.robots import bus_status

    with pytest.raises(HTTPException) as err:
        asyncio.run(bus_status(minutes=7))
    assert err.value.status_code == 400
