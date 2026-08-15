"""게이트웨이 재시작 후 유닛 재부착 — 정책 서버·작업 유닛.

배경: 게이트웨이를 재시작하자 `piper-policysrv` 유닛은 :8088 에서 계속 도는데
`/api/policy-server/status` 는 `state=idle` (pid 는 유닛 PID)로 답했다.
activity 에서 빠지니 **배타 모드 가드가 정책 서버를 고려하지 않는다** —
학습은 `restore_running_process()` 로 이미 고쳐진 문제(ROADMAP 3.5)인데
정책 서버·업로드·편집·페이즈 유닛에는 재부착 호출이 없었다.
"""

from app.services.policy_server_manager import PolicyServerManager
from app.services.process_manager import ProcessManager, ProcessState


class FakeUnit:
    """`SystemdProcess` 의 재부착 표면만 흉내낸다."""

    def __init__(self, active: bool, argv: list[str] | None = None):
        self._active = active
        self._argv = argv or []
        self._state = ProcessState.IDLE
        self.pid = 4242 if active else None

    @property
    def state(self) -> ProcessState:
        return self._state

    def reattach(self) -> bool:
        if not self._active:
            return False
        self._state = ProcessState.RUNNING
        return True

    def exec_argv(self) -> list[str]:
        return self._argv


# ── 정책 서버 ──

def _manager(pm) -> PolicyServerManager:
    m = PolicyServerManager.__new__(PolicyServerManager)
    m.pm = pm
    m.host, m.port, m.fps = "127.0.0.1", 8088, 30
    return m


def test_restore_reattaches_and_recovers_exec_args():
    """상태만 복구하면 주소가 기본값으로 남는다 — ExecStart 가 진실이다.

    실측: 유닛은 `--host=0.0.0.0` 으로 떴는데 status 는 127.0.0.1 을 보였다.
    """
    m = _manager(FakeUnit(True, [
        "python", "-u", "start_policy_server.py",
        "--host=0.0.0.0", "--port=9000", "--fps=20",
    ]))
    assert m.restore_running_process() is True
    assert m.state == ProcessState.RUNNING
    assert (m.host, m.port, m.fps) == ("0.0.0.0", 9000, 20)


def test_restore_without_live_unit_changes_nothing():
    m = _manager(FakeUnit(False))
    assert m.restore_running_process() is False
    assert m.state == ProcessState.IDLE
    assert (m.host, m.port, m.fps) == ("127.0.0.1", 8088, 30)


def test_restore_is_a_noop_for_child_process_runner():
    """`process_runner=local` 이면 자식 프로세스라 재부착할 것이 없다 — 조용히 False."""
    m = _manager(ProcessManager())
    assert m.restore_running_process() is False


def test_restore_survives_malformed_exec_args():
    """이상한 인자가 있어도 복구가 죽으면 안 된다 — 붙는 것이 우선이다."""
    m = _manager(FakeUnit(True, ["python", "--port=notanumber", "--fps", "--host=0.0.0.0"]))
    assert m.restore_running_process() is True
    assert m.host == "0.0.0.0"
    assert m.port == 8088  # 못 읽은 값은 기본값 유지


# ── 작업 유닛 (업로드·편집·페이즈) ──

def test_dataset_jobs_restore_reports_reattached_units(monkeypatch):
    from app.services import dataset_jobs

    monkeypatch.setattr(dataset_jobs, "upload_pm", FakeUnit(True))
    monkeypatch.setattr(dataset_jobs, "edit_pm", FakeUnit(False))
    monkeypatch.setattr(dataset_jobs, "phase_pm", ProcessManager())  # local 러너
    assert dataset_jobs.restore_running_jobs() == ["upload"]


def test_startup_calls_every_restore():
    """기동 시퀀스가 셋 다 부르는지 — 하나라도 빠지면 이 버그가 되돌아온다."""
    import inspect

    from app import main

    src = inspect.getsource(main.lifespan)
    assert "train_manager.restore_running_process()" in src
    assert "policy_server_manager.restore_running_process()" in src
    assert "dataset_jobs.restore_running_jobs()" in src
