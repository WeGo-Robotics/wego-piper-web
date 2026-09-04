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

    from piper_robot.hub import RobotHub

    src = python_code_only(textwrap.dedent(inspect.getsource(RobotHub.bus_reset)))
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

    ui = code_only(PANEL.read_text())
    assert "/ dt" in ui, "누적값을 그대로 그린다"


def test_the_two_traffic_series_share_one_axis():
    """⚠ 이중 축 금지 — RX 와 TX 는 같은 단위(패킷/초)라 축을 쪼개면 크기 비교가
    거짓이 된다. TX 가 바닥에 붙는 것은 사실이고, 대신 최신값을 숫자로 붙인다."""
    from pathlib import Path

    from conftest import code_only as _c

    charts = _c((Path(__file__).resolve().parents[2] / "frontend" / "src"
                 / "components" / "BusCharts.tsx").read_text())
    assert charts.count("const max = Math.max(1, ...points") == 1, "축이 둘이다"
    assert "p.rx, p.tx" in charts, "두 계열이 같은 최대값을 안 쓴다"
