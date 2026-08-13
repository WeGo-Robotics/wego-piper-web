"""systemd 사용자 유닛으로 프로세스를 띄운다 — `ProcessManager` 의 짝.

`ProcessManager` 는 subprocess 를 게이트웨이 **자식**으로 들고 있어서, 서버가
재시작되면 프로세스는 계속 도는데 화면에서 사라진다. 학습이 그 문제로 오래
고생했고(ROADMAP 3b-6), 정책 서버·업로드처럼 오래 도는 것들도 사정이 같다.

유닛으로 띄우면 소유자가 systemd 다:

- 게이트웨이 재시작·크래시와 무관하게 산다
- 상태는 `systemctl is-active` 가 답한다 — PID 파일이 필요 없다
- 로그는 journald 에 남아 **재부착 때 처음부터 다시 읽는다**

## `--user` 를 쓰는 이유

시스템 유닛으로 띄우면 root 로 돌아 산출물 소유자가 어긋난다.

⚠ 사용자 유닛은 **로그아웃하면 함께 죽는다.** 이 프로젝트가 이미 겪었고
(`loginctl enable-linger`), 그 전제 위에서만 의미가 있다. `available()` 이 확인한다.

## `--scope` 를 쓰지 않는다

호출자의 cgroup 에 들어가 게이트웨이와 함께 죽는다 — 유닛이 소유자가 되는
것이 이 모듈의 존재 이유다.
"""

import asyncio
import contextlib
import logging
import shutil
import subprocess
from collections.abc import Callable

from app.services.process_manager import ProcessState

logger = logging.getLogger(__name__)


# 유닛 이름 접두사. 우리 것만 골라내 남의 유닛을 건드리지 않는다.
UNIT_PREFIX = "piper-"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True, timeout=10)


def available() -> tuple[bool, str]:
    """이 러너를 쓸 수 있는가. **못 쓰면 사유를 말한다.**

    조용히 `LocalRunner` 로 떨어지면 "재시작해도 학습이 살아있다"고 믿는데
    실제로는 아닌 상태가 된다 — 그게 가장 나쁜 결과다.
    """
    if not shutil.which("systemd-run"):
        return False, "systemd-run 이 없습니다"
    try:
        if _systemctl("--version").returncode != 0:
            return False, "사용자 systemd 에 접속할 수 없습니다"
    except Exception as exc:
        return False, f"systemd 확인 실패: {exc}"
    # ⚠ linger 가 꺼져 있으면 로그아웃 시 학습이 죽는다 — 이 러너를 쓰는 의미가 없다
    try:
        import getpass

        out = subprocess.run(["loginctl", "show-user", getpass.getuser(),
                              "--property=Linger"],
                             capture_output=True, text=True, timeout=10).stdout
        if "Linger=yes" not in out:
            return False, ("linger 가 꺼져 있어 로그아웃 시 학습이 함께 죽습니다 — "
                           "`loginctl enable-linger` 로 켜세요")
    except Exception as exc:
        logger.warning("linger 확인 실패: %s", exc)
    return True, "OK"

class SystemdProcess:
    """유닛 하나의 수명. `ProcessManager` 와 같은 표면을 노출한다."""

    def __init__(self, unit: str) -> None:
        self.unit = unit
        self._state = ProcessState.IDLE
        self._on_log: Callable[[str], None] | None = None
        self._on_state: Callable[[ProcessState], None] | None = None
        self._log_task: asyncio.Task | None = None

    # ── 상태 ──

    @property
    def state(self) -> ProcessState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state in (ProcessState.RUNNING, ProcessState.STARTING)

    @property
    def pid(self) -> int | None:
        """유닛의 메인 PID. **진단용이다** — 상태 판정은 systemd 가 한다."""
        out = _systemctl("show", self.unit, "--property=MainPID").stdout.strip()
        try:
            pid = int(out.split("=", 1)[1])
        except (IndexError, ValueError):
            return None
        return pid or None

    def _set_state(self, state: ProcessState) -> None:
        self._state = state
        if self._on_state:
            self._on_state(state)

    def set_log_callback(self, cb: Callable[[str], None]) -> None:
        self._on_log = cb

    def set_state_callback(self, cb: Callable[[ProcessState], None]) -> None:
        self._on_state = cb

    # ── 실행 ──

    async def start(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
        ok, why = available()
        if not ok:
            raise RuntimeError(f"systemd 러너를 쓸 수 없습니다: {why}")
        # 지난 실행이 실패로 남아 있으면 같은 이름으로 못 띄운다
        _systemctl("reset-failed", self.unit)

        argv = [
            "systemd-run", "--user", f"--unit={self.unit}",
            "--property=Type=exec",
            # ⚠ 게이트웨이가 죽어도 유닛은 살아야 한다 — 그게 이 러너의 존재 이유다.
            #   `--scope` 를 쓰면 호출자의 cgroup 에 들어가 함께 죽는다.
            "--collect",
        ]
        for k, v in (env or {}).items():
            cmd += [f"--setenv={k}={v}"]
        argv += ["--"] + list(cmd)

        self._set_state(ProcessState.STARTING)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            self._set_state(ProcessState.ERROR)
            raise RuntimeError(f"유닛 시작 실패: {(out or b'').decode(errors='replace')}")

        self._set_state(ProcessState.RUNNING)
        self._start_log_stream()
        logger.info("학습 유닛 시작: %s", self.unit)

    async def stop(self) -> None:
        _systemctl("stop", self.unit)
        _systemctl("reset-failed", self.unit)
        if self._log_task:
            self._log_task.cancel()
            self._log_task = None
        self._set_state(ProcessState.IDLE)

    # ── 로그 ──

    def _start_log_stream(self, follow_from_start: bool = False) -> None:
        """journald 를 따라 읽어 콜백으로 넘긴다.

        `follow_from_start` 는 **재부착할 때** 쓴다 — 게이트웨이가 없던 동안의
        로그를 처음부터 다시 읽어야 화면의 진행률이 이어진다.
        stdout 을 못 되돌리던 `LocalRunner` 와 갈리는 지점이다.
        """
        if self._log_task and not self._log_task.done():
            return

        async def _pump() -> None:
            args = ["journalctl", "--user", "-u", self.unit, "-f", "-o", "cat"]
            args += ["--lines=all"] if follow_from_start else ["--lines=0"]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
            except Exception as exc:
                logger.warning("journald 연결 실패 (%s): %s", self.unit, exc)
                return
            try:
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").rstrip("\n")
                    if line and self._on_log:
                        self._on_log(line)
            except asyncio.CancelledError:
                raise
            finally:
                with contextlib.suppress(Exception):
                    proc.terminate()

        self._log_task = asyncio.create_task(_pump())

    # ── 복원 ──

    def reattach(self) -> bool:
        """살아있는 유닛에 **다시 붙는다.** 붙었으면 True.

        PID 파일을 보지 않는다 — systemd 가 진실이다. 로그도 journald 에서
        처음부터 다시 읽으므로 없던 동안의 진행이 화면에 채워진다.
        """
        if _systemctl("is-active", self.unit).stdout.strip() != "active":
            return False
        self._set_state(ProcessState.RUNNING)
        self._start_log_stream(follow_from_start=True)
        logger.info("유닛 재부착: %s", self.unit)
        return True


_availability: tuple[bool, str] | None = None


def make_process(unit: str):
    """설정이 고른 프로세스 소유자. `ProcessManager` 자리에 그대로 들어간다.

    ⚠ **못 쓰면 조용히 떨어지지 않고 말한다.** 조용히 자식 프로세스로 가면
    "재시작해도 살아있다"고 믿는데 실제로는 아닌 상태가 된다 — 그게 가장 나쁘다.

    가용성 판정은 한 번만 한다. 모듈 로드 때 여러 소유자가 만들어지는데
    그때마다 `systemctl`·`loginctl` 을 부르면 기동이 그만큼 느려진다.
    """
    global _availability
    from app.core.config import settings
    from app.services.process_manager import ProcessManager

    if settings.process_runner != "systemd":
        return ProcessManager()

    if _availability is None:
        _availability = available()
    ok, why = _availability
    if not ok:
        logger.warning("systemd 를 쓸 수 없어 자식 프로세스로 돕니다 (%s) — %s "
                       "(게이트웨이를 재시작하면 화면에서 사라집니다)", unit, why)
        return ProcessManager()
    return SystemdProcess(unit)
