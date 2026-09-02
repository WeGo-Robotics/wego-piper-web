"""대시보드 — **"지금 괜찮은가"** 하나에만 답한다.

예전 대시보드는 사이드바와 똑같은 목적지를 카드로 반복했다. 이동 수단이 둘일
이유가 없고, 그 자리에 상태를 놓으면 클릭 없이 답이 보인다.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
DASH = _SRC / "pages" / "DashboardPage.tsx"
PAGES = _SRC / "config" / "pages.ts"


@pytest.fixture
def client():
    return TestClient(app)


def test_the_dashboard_is_not_a_second_menu():
    """⚠ 카드 그리드는 사이드바를 그대로 반복했다 — 이동 수단이 둘일 이유가 없다."""
    src = DASH.read_text()
    assert "cardPages" not in src, "대시보드가 다시 메뉴가 됐다"


def test_no_page_is_reachable_only_through_the_dashboard():
    """⚠ **카드를 걷어내면서 길이 끊길 수 있다.** 예전에 상단 내비에 자리가 없어
    `card` 만 달고 `nav` 가 없던 페이지가 있었고(모델·데이터셋), 그때는 대시보드
    카드가 유일한 입구였다. 그런 페이지가 다시 생기면 이 검사가 잡는다.
    """
    blocks = re.findall(r"\{[^{}]*?\}", PAGES.read_text(), re.S)
    orphan = []
    for b in blocks:
        m = re.search(r"label:\s*'([^']+)'", b)
        if not m:
            continue
        if re.search(r"\bcard:\s*true", b) and not re.search(r"\bnav:\s*true", b):
            orphan.append(m.group(1))
    assert not orphan, f"대시보드 카드로만 갈 수 있는 페이지: {orphan}"


def test_the_dashboard_leads_with_what_is_wrong():
    """평소엔 맨 위 한 줄만 보고 지나가고, 문제가 있을 때만 아래를 본다.
    그 줄이 없으면 사람이 네 상자를 매번 훑어야 한다."""
    src = DASH.read_text()
    assert "problems" in src, "문제 요약이 없다"
    for signal in ("stale", "alerts", "free_gb"):
        assert signal in src, f"요약이 {signal} 를 안 본다"


def test_it_shows_what_actually_breaks_here():
    """항목을 지어내지 않는다 — 이 시스템이 실제로 고장난 방식에서 골랐다.

    유닛이 옛 코드로 도는 것(하루 두 번 겪었다), 장치가 사라지는 것,
    배타 모드로 시작이 막히는 것, GPU 경합, 디스크.
    """
    src = DASH.read_text()
    for must in ("useSystemStatus", "useDeviceSummary", "useActivity"):
        assert must in src, f"{must} 를 안 쓴다"
    assert "E-stop" in src, "안전 상태가 없다"


def test_estop_without_a_bus_is_unknown_not_fine():
    """⚠ **안전 표시가 거짓말하면 안 된다.** 버스가 없으면 E-stop 이 동작할지
    알 수 없다 — 그걸 초록으로 그리면 없는 안전을 있다고 말하는 것이다."""
    # ⚠ 첫 "E-stop" 은 **주석**이고 판정은 그 뒤에 온다 — 앞을 자르면 못 본다.
    #   주석과 코드가 섞인 파일에서 위치로 구간을 자를 때 흔한 실수다.
    src = DASH.read_text()
    i = src.index("E-stop")
    block = src[max(0, i - 300):i + 600]
    assert "bus_available" in block, "버스 유무를 안 본다"
    assert "버스 없음" in block, "버스가 없을 때 뭐라고 할지가 없다"


def test_resources_survive_a_stuck_driver(client):
    """⚠ `nvidia-smi` 는 드라이버가 걸리면 D-state 로 멈춘다 — D405 의 UVC 질의로
    똑같이 겪었고 그때 **이벤트 루프 전체가 먹통**이 됐다. 대시보드 하나 때문에
    웹이 안 뜨는 일은 없어야 한다.
    """
    from app.services import resources

    assert resources.NVIDIA_SMI_TIMEOUT_S <= 5, "타임아웃이 너무 길다"
    src = (Path(resources.__file__)).read_text()
    assert "timeout=" in src, "nvidia-smi 에 타임아웃이 없다"
    router = (Path(__file__).resolve().parents[1] / "app" / "routers" / "system.py").read_text()
    assert "to_thread" in router, "이벤트 루프에서 직접 부른다"

    r = client.get("/api/system/resources")
    assert r.status_code == 200
    assert set(r.json()) == {"gpus", "disks", "cpu_pct", "samples"}


def test_a_machine_without_a_gpu_is_not_an_error(client):
    """GPU 없는 기계도 있다. 없는 것과 못 읽는 것을 에러로 만들면 그 기계에서는
    대시보드가 통째로 빈다."""
    from app.services import resources

    src = (Path(resources.__file__)).read_text()
    assert "FileNotFoundError" in src, "nvidia-smi 가 없는 경우를 안 다룬다"
    assert "return []" in src, "실패를 예외로 올린다"


def test_the_trend_graph_is_full_on_page_load(client):
    """⚠ 추이는 **서버가 쌓는다.** 브라우저가 쌓으면 페이지를 연 순간부터만
    보이는데, 그래프를 보러 오는 이유는 대개 "아까 무슨 일이 있었나"다.
    `since` 없이 부르면 창 전체가 오고, `since` 를 주면 새 견본만 온다.
    """
    from app.services import trends

    old = list(trends._samples)
    trends._samples.clear()
    try:
        trends._samples.extend([
            {"t": 100.0, "cpu_pct": 10.0, "fps": None, "gpus": []},
            {"t": 104.0, "cpu_pct": 20.0, "fps": 29.8, "gpus": []},
        ])
        full = client.get("/api/system/resources").json()
        assert [s["t"] for s in full["samples"]] == [100.0, 104.0], "로딩 때 창 전체가 안 온다"
        inc = client.get("/api/system/resources", params={"since": 100.0}).json()
        assert [s["t"] for s in inc["samples"]] == [104.0], "since 이후 것만 와야 한다"
    finally:
        trends._samples.clear()
        trends._samples.extend(old)


def test_a_sample_records_what_the_charts_draw():
    """견본 한 점에는 CPU·GPU(사용률+메모리%)·추론 fps 가 다 들어간다 —
    차트 세 개가 같은 버퍼를 그린다. fps 는 텔레메트리가 **최근에** 지나갔을
    때만 값이고, 멈춘 지 오래면 None 이라 그래프가 거기서 끊긴다."""
    import time as _time

    from app.services import trends

    trends.note_fps(30.2)
    s = trends._take_sample()
    assert set(s) == {"t", "cpu_pct", "fps", "gpus"}
    assert s["fps"] == 30.2
    # 오래된 fps 는 줍지 않는다
    trends._last_fps = (_time.monotonic() - trends.SAMPLE_INTERVAL_S * 3, 30.2)
    assert trends._take_sample()["fps"] is None
    trends._last_fps = None


def test_the_sampler_does_not_double_the_nvidia_smi_calls():
    """⚠ nvidia-smi 는 드라이버가 걸리면 멈추는 물건이라 **호출 횟수가 곧
    위험**이다. 샘플러가 4초마다 이미 부르는데 대시보드 폴링이 또 부르면
    두 배로 찌른다 — 라우터는 샘플러의 마지막 견본을 재사용해야 한다."""
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "system.py").read_text()
    assert "latest_gpus()" in src, "라우터가 nvidia-smi 를 직접만 부른다"


def test_one_failed_call_does_not_blank_the_dashboard():
    """⚠ 하나가 실패해도 나머지는 그린다. 자원 조회가 막혔다고 서비스 상태까지
    못 보면, 정작 문제가 났을 때 화면이 통째로 빈다."""
    hook = (_SRC / "hooks" / "useSystemStatus.ts").read_text()
    assert hook.count(".catch(() => null)") >= 3, "실패를 개별로 안 삼킨다"
    assert "prev." in hook, "직전 값을 안 지킨다 — 화면이 깜빡인다"
