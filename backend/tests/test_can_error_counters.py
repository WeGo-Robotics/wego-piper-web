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
