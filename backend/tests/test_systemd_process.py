"""유닛 러너 — **유닛은 스스로 끝나도 아무도 안 알려준다.**

`ProcessManager`(자식 프로세스)는 종료를 즉시 안다. systemd 유닛은 다르다: 끝났는지는
`systemctl is-active` 에게 물어봐야 알고, 그 물음은 `state` 를 **읽을 때만** 일어난다.

⚠ 실기(2026-09-16): Hub 업로드가 `✓ Uploaded` 로 끝나고 유닛도 `inactive/success` 인데
화면은 계속 `running` 이고 [중지] 버튼이 남아 [닫기] 로 안 바뀌었다. 업로드 모달은 WS
푸시(`upload_state`)만 보고 따로 폴링하지 않으니, 끝난 걸 아무도 안 읽어 아무도 몰랐다.
"""

import threading

import pytest

from app.services import systemd_process as S
from app.services.process_manager import ProcessState


class _Result:
    def __init__(self, out: str) -> None:
        self.stdout, self.stderr, self.returncode = out, "", 0


@pytest.fixture
def unit(monkeypatch):
    """`is-active` 가 처음엔 active, 그다음부터 inactive 를 답하는 유닛."""
    calls = {"n": 0}

    def fake_systemctl(*args):
        if args and args[0] == "is-active":
            calls["n"] += 1
            return _Result("active\n" if calls["n"] <= 1 else "inactive\n")
        return _Result("")

    monkeypatch.setattr(S, "_systemctl", fake_systemctl)
    p = S.SystemdProcess("piper-test-unit")
    p._state = ProcessState.RUNNING
    yield p
    p._stop_watch()


def test_a_unit_that_finishes_on_its_own_tells_the_screen_without_being_asked(unit):
    """감시 스레드는 주기적으로 `state` 를 **읽어 주기만** 한다 — 판정도 콜백도 그대로다.
    그 한 번의 읽기가 없어서 화면이 영원히 "업로드 중" 이었다."""
    seen: list[ProcessState] = []
    done = threading.Event()
    unit.set_state_callback(lambda st: (seen.append(st), done.set()))

    unit._start_watch(every=0.01)

    assert done.wait(3.0), "유닛이 끝났는데 아무도 화면에 안 알렸다"
    assert seen[-1] is ProcessState.IDLE, f"정상 종료를 idle 로 안 내린다: {seen}"
    assert not unit.is_running


def test_the_watch_starts_with_the_unit_and_stops_with_the_logs():
    """시작·재부착에서 켜고, 상태가 내려가며 로그 스트림을 멈출 때 같이 멈춘다 —
    끝난 유닛을 계속 지켜보는 스레드가 남으면 그것대로 샌다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "services" / "systemd_process.py").read_text()
    start = src.split("async def start", 1)[1].split("async def stop", 1)[0]
    assert "self._start_watch()" in start, "시작할 때 감시를 안 켠다"
    reattach = src.split("def reattach", 1)[1]
    assert "self._start_watch()" in reattach, "재부착한 유닛은 끝나도 안 알린다"
    stop_logs = src.split("def _stop_log_stream", 1)[1].split("def ", 1)[0]
    assert "self._stop_watch()" in stop_logs, "전이 뒤에도 감시 스레드가 남는다"
