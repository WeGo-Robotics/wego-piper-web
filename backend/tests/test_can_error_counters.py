"""CAN 오류 카운터 감시.

## 왜 상태 조회로는 부족한가

`can_state()` 는 **지금** 나쁜지만 본다. 잠깐 `ERROR-PASSIVE` 로 내려갔다
돌아오는 버스는 물어보는 순간마다 늘 `ERROR-ACTIVE` 라 영영 안 걸린다.

실측(2026-08-28, 양팔):

    can2  3-6.3     열거 08:36:50   error_passive       0
    can3  3-6.4.1   열거 08:36:51   error_passive  34,794

1초 차이로 올라온 두 인터페이스가 이만큼 갈렸는데, 상태 조회로는 한 번도
안 걸렸다. **"통신이 좀 불안정한 것 같다"를 숫자로 바꾸는 자리다.**
"""

import pytest

pytest.importorskip("piper_robot")
from piper_robot.can import ERROR_COUNTERS, error_counters  # noqa: E402
from piper_robot.publish import ArmBridge  # noqa: E402


def test_the_counter_names_match_the_kernel_line():
    """`ip` 출력의 열 순서 그대로여야 한다 — 어긋나면 조용히 엉뚱한 수를 읽는다."""
    assert ERROR_COUNTERS == ("restarts", "bus_errors", "arbitration_lost",
                              "error_warning", "error_passive", "bus_off")


def test_the_ports_card_reads_bus_stats_from_robotd_not_from_the_container():
    """⚠ 실기(NUC, 2026-09-15): sudoers 를 고쳐 CAN 이 **실제로** 올라왔는데도 포트 카드의
    bitrate·can_state·Rx/Tx 가 전부 비어 "UP 을 눌러도 초기화가 안 된다"로 읽혔다.

    게이트웨이는 컨테이너라 `ip` 도 `/sys/class/net` 도 못 본다 — 그 자리에서 `bus_stats` 를
    직접 부르면 언제나 null 이고, 카드는 링크만 `arm.state` 폴백으로 "UP" 을 보여 준다.
    같은 순간 robotd 는 `bitrate 1000000 · ERROR-ACTIVE · healthy` 로 보고했다
    (`/robots/bus` 는 처음부터 robotd 를 거친다). 같은 규칙을 포트 카드에도 적용한다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "robots.py").read_text()
    body = src.split("async def list_ports", 1)[1].split("\n@router", 1)[0]
    assert '_call, "bus_status"' in body, "포트 카드가 robotd 에 안 묻는다 — 컨테이너에선 늘 빈칸이다"
    assert "by_iface.get(iface) or await asyncio.to_thread(bus_stats, iface)" in body, \
        "robotd 가 없을 때의 지역 폴백이 없다 (저장소에서 직접 띄운 게이트웨이)"
    assert body.index('_call, "bus_status"') < body.index("for iface, arm in"), \
        "인터페이스마다 RPC 를 친다 — 폴링되는 자리라 한 번만 물어야 한다"


def test_bringing_a_can_interface_down_goes_through_robotd_like_bringing_it_up():
    """⚠ 실기(NUC, 2026-09-15): 포트 카드의 [DOWN] 이 `bring-down failed: Cannot find device
    "can0"` 로 끝났다. 그 순간 호스트의 can0 은 `UP · bitrate 1000000 · ERROR-ACTIVE` 로
    멀쩡했고, **컨테이너 안에만** 없었다(브리지 네트워크라 `ip link show can0` 이
    `Device "can0" does not exist.`).

    UP 은 처음부터 `init_interface` RPC 로 robotd 를 거쳤는데 DOWN 만 빠져 있었다 —
    같은 버튼 줄의 두 동작이 서로 다른 기계에서 돌고 있었다."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    body = (root / "backend" / "app" / "routers" / "robots.py").read_text() \
        .split("async def can_down", 1)[1].split("\n@router", 1)[0]
    assert "from piper_robot.can import down_can_interface" not in body, \
        "DOWN 이 컨테이너 안에서 ip 를 부른다 — 거기엔 can0 이 없다"
    assert "from app.services.robot_manager import down_can_interface" in body
    assert '_call("down_interface"' in (root / "backend" / "app" / "services" / "robot_manager.py").read_text(), \
        "게이트웨이 래퍼가 robotd 를 안 거친다"
    assert '"down_interface"' in (root / "daemons" / "robotd.py").read_text(), \
        "robotd 화이트리스트에 없다 — RPC 가 '알 수 없는 메서드' 로 거절된다"
    assert "def down_interface" in (root / "robot" / "piper_robot" / "hub.py").read_text(), \
        "robotd 허브에 동사가 없다"


def test_the_teleop_bus_guard_asks_robotd_instead_of_failing_open():
    """⚠ 배포판(컨테이너)에서는 `can_state` 가 아무것도 못 읽어 `can_unhealthy_reason` 이
    **늘 None(=정상)** 을 돌려준다 — 조작 시작 가드가 조용히 통과한다. 그러면 BUS-OFF 인데도
    조그가 열리고 슬라이더는 움직이는데 팔만 안 움직인다. 그 가드를 둔 이유가 바로 그
    상황인데, 배포판에서만 되살아나 있었다 (NUC 점검 2026-09-15).

    robotd 에게 묻는다. robotd 가 없으면 예전처럼 통과다 — 가드가 없다고 조작을 막지는 않는다."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    guard = (root / "backend" / "app" / "services" / "teleop.py").read_text() \
        .split("def require_healthy_bus", 1)[1].split("\ndef ", 1)[0]
    assert "from piper_robot.can import can_unhealthy_reason" not in guard, \
        "가드가 컨테이너에서 CAN 상태를 읽는다 — 늘 '정상' 이 된다"
    assert "from app.services.robot_manager import can_unhealthy_reason" in guard
    mgr = (root / "backend" / "app" / "services" / "robot_manager.py").read_text()
    assert '_call("unhealthy_reason"' in mgr and "default=None" in mgr, \
        "래퍼가 robotd 를 안 거치거나, 데몬이 없을 때 통과하지 않는다"
    assert '"unhealthy_reason"' in (root / "daemons" / "robotd.py").read_text(), \
        "robotd 화이트리스트에 없다 — RPC 가 '알 수 없는 메서드' 로 거절된다"
    assert "def unhealthy_reason" in (root / "robot" / "piper_robot" / "hub.py").read_text()


def test_an_unknown_interface_returns_empty_not_zero():
    """⚠ 0 을 돌려주면 **못 읽은 것과 깨끗한 것이 구별되지 않는다.**"""
    assert error_counters("can_does_not_exist") == {}


def test_only_growth_is_logged():
    """절대값은 인터페이스를 다시 열면 0 이 된다 — 증가분만 뜻이 있다."""
    import inspect

    src = inspect.getsource(ArmBridge._sample_can_errors)
    assert "now[k] - before[k]" in src
    assert "if before is None:" in src, "첫 표본을 증가로 읽으면 매번 경고가 뜬다"


def test_the_first_sample_is_not_an_alarm():
    """기동 직후 34,794 를 보고 "늘었다"고 하면 안 된다 — 비교 대상이 없다."""
    b = ArmBridge.__new__(ArmBridge)
    b.iface = "can_does_not_exist"
    b._err_counters = None
    b._sample_can_errors()          # 읽기 실패 → 조용히 넘어간다
    assert b._err_counters is None


def test_it_does_not_run_every_frame():
    """`ip` 호출이 3~4ms 다 — 프레임마다 부르면 그 자체가 부하다."""
    assert ArmBridge.ERR_SAMPLE_S >= 5.0


def test_the_warning_says_what_to_check():
    """"오류가 늘었다" 만으로는 다음에 뭘 할지 모른다."""
    import inspect

    src = inspect.getsource(ArmBridge._sample_can_errors)
    assert "케이블" in src and "허브" in src


@pytest.mark.parametrize("iface", ["can0", "can1", "can2", "can3"])
def test_it_reads_a_real_interface_when_present(iface):
    """실기에서만 도는 확인 — 없으면 건너뛴다."""
    from pathlib import Path

    if not Path(f"/sys/class/net/{iface}").exists():
        pytest.skip(f"{iface} 없음")
    got = error_counters(iface)
    assert set(got) == set(ERROR_COUNTERS), f"{iface} 파싱 실패: {got}"
