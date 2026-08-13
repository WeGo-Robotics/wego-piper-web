"""학습 러너 — `LocalRunner` / `SystemdRunner` 계약 (ROADMAP 3b-6).

두 러너는 `TrainRunner` 이음매를 통해 서로 갈아끼워진다. **표면이 갈리면
설정을 바꾼 순간 런타임에 터진다** — 그것도 학습을 시작하려는 순간에.
"""

import inspect

from app.services.training.runners.base import TrainRunner
from app.services.training.runners.local import LocalRunner
from app.services.systemd_process import SystemdProcess
from app.services.training.runners.systemd import SystemdRunner

# 유닛 기계장치는 `SystemdProcess` 가 갖고, 러너는 학습 고유의 것만 갖는다.
# 그래서 "유닛을 어떻게 띄우는가" 검사는 그쪽을 본다.


def _surface(cls) -> set[str]:
    return {n for n in dir(cls) if not n.startswith("_")}


def test_both_runners_implement_the_same_seam():
    """프로토콜이 요구하는 것을 둘 다 가져야 한다."""
    required = {n for n in dir(TrainRunner) if not n.startswith("_")}
    for runner in (LocalRunner, SystemdRunner):
        missing = required - _surface(runner)
        assert not missing, f"{runner.__name__} 에 없는 것: {sorted(missing)}"


def test_async_methods_stay_async_on_both():
    """한쪽만 동기면 `await` 하는 호출부가 깨진다."""
    for name in ("start", "stop"):
        assert inspect.iscoroutinefunction(getattr(LocalRunner, name))
        assert inspect.iscoroutinefunction(getattr(SystemdRunner, name)), (
            f"SystemdRunner.{name} 이 async 가 아니다"
        )


def test_systemd_runner_never_uses_scope():
    """`--scope` 는 호출자의 cgroup 에 들어가 **게이트웨이와 함께 죽는다.**

    유닛이 소유자가 되는 것이 이 러너의 존재 이유다.
    """
    # **주석이 아니라 실제 인자**를 본다 — 설명문에 `--scope` 가 나온다
    import ast

    tree = ast.parse(inspect.getsource(SystemdProcess.start).lstrip())
    args = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value.startswith("--")}
    assert "--scope" not in args, "--scope 를 쓰면 게이트웨이가 죽을 때 학습도 죽는다"
    assert any(a.startswith("--unit") for a in args), "유닛 이름을 안 준다"


def test_unavailable_systemd_is_loud_not_silent():
    """조용히 local 로 떨어지면 "재시작해도 살아있다"고 믿게 된다.

    `available()` 이 사유를 돌려주고, `start()` 는 못 쓰면 예외를 던진다.
    """
    from app.services import systemd_process as systemd

    ok, why = systemd.available()
    assert isinstance(ok, bool) and why, "사유 없이 판정만 돌려준다"

    src = inspect.getsource(SystemdProcess.start)
    assert "raise RuntimeError" in src, "못 쓰는데 조용히 시작한 척한다"


def test_linger_is_part_of_availability():
    """linger 가 꺼져 있으면 로그아웃 시 학습이 함께 죽는다 —
    그 상태로 systemd 러너를 쓰면 local 과 다를 게 없다."""
    from app.services import systemd_process as systemd

    assert "Linger" in inspect.getsource(systemd.available)


def test_restore_reattaches_without_a_pid_file():
    """systemd 가 진실이다. PID 파일은 재부팅에 날아가고 재사용 위험도 있다."""
    src = inspect.getsource(SystemdProcess.reattach)
    assert "is-active" in src
    assert "PID_FILE" not in src and "/tmp/" not in src


def test_restore_replays_the_log():
    """게이트웨이가 없던 동안의 로그를 다시 읽어야 진행률이 이어진다.

    stdout 을 못 되돌리던 `LocalRunner` 와 갈리는 지점이라 여기서 잠근다.
    """
    src = inspect.getsource(SystemdProcess.reattach)
    assert "follow_from_start=True" in src


# ── 공용 프로세스 소유자 (학습 밖에서도 쓴다) ──

def test_systemd_process_matches_the_process_manager_surface():
    """`SystemdProcess` 는 `ProcessManager` 자리에 그대로 들어가야 한다.

    정책 서버는 `self.pm` 을 설정에 따라 둘 중 하나로 받는다. 표면이 갈리면
    **설정을 바꾼 순간 런타임에 터진다** — 서버를 시작하려는 그 순간에.
    """
    from app.services.process_manager import ProcessManager

    # `is_running` 은 매니저가 `state` 로 계산하므로 `ProcessManager` 에는 없다.
    # 갈아끼우는 데 필요한 것은 **양쪽에 다 있는 것**이어야 한다.
    required = {"state", "pid", "set_log_callback", "start", "stop"}
    for owner in (SystemdProcess, ProcessManager):
        missing = required - _surface(owner)
        assert not missing, f"{owner.__name__} 에 없는 것: {sorted(missing)}"


def test_long_running_processes_pick_their_owner_from_one_place():
    """하나는 유닛이고 하나는 자식이면 재시작 동작이 갈려 더 헷갈린다 —
    오래 도는 것들이 **같은 스위치, 같은 함수**를 따른다."""
    import ast

    from app.services import systemd_process as sp

    src = inspect.getsource(sp.make_process)
    names = {ast.unparse(n) for n in ast.walk(ast.parse(src.lstrip()))
             if isinstance(n, ast.Attribute)}
    assert "settings.process_runner" in names, "스위치를 안 본다"
    assert "raise" not in src, "시작을 막지 않는다 — 못 쓰면 자식으로 떨어진다"
    assert "logger.warning" in src, "조용히 떨어지면 재시작 생존을 오해한다"


def test_every_long_running_owner_goes_through_make_process():
    """직접 `ProcessManager()` 를 만들면 그 프로세스만 재시작에 사라진다.

    녹화·추론은 예외다 — 3b-7 에서 컨테이너로 가는 쪽이라 성격이 다르다.
    `LocalRunner` 도 예외다: systemd 의 **대안**이라 직접 만드는 게 맞다.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "services"
    allowed = {"local.py", "record_manager.py", "process_manager.py", "systemd_process.py"}
    offenders = []
    for f in root.rglob("*.py"):
        if f.name in allowed:
            continue
        if re.search(r"(?<!def )ProcessManager\(\)", f.read_text()):
            offenders.append(f.name)
    assert not offenders, f"make_process 를 안 거치는 곳: {offenders}"
