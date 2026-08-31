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
import threading
import shutil
import subprocess
from collections.abc import Callable

from app.services.process_manager import ProcessState, injected_env

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
        self._log_thread: threading.Thread | None = None
        self._log_proc: subprocess.Popen | None = None
        self._log_stop = threading.Event()
        # 이 게이트웨이가 이 유닛을 띄운 적이 있는가. 없으면 저널의 옛 줄은
        # **남의 실행 기록**이라 사유로 보여주면 안 된다.
        self._started_once = False

    # ── 상태 ──

    @property
    def state(self) -> ProcessState:
        """유닛에게 **물어본다.** 캐시를 돌려주면 안 된다.

        ⚠ `ProcessManager` 는 자식이라 종료를 즉시 알지만, 유닛은 스스로 끝나도
        아무도 안 알려준다. 캐시만 보면 학습이 끝난 뒤에도 `running` 으로 남아
        **다음 학습이 "이미 실행 중입니다" 로 막힌다** — 실기에서 걸렸다.
        """
        if self._state in (ProcessState.RUNNING, ProcessState.STARTING):
            active = _systemctl("is-active", self.unit).stdout.strip()
            if active != "active":
                # `failed` 는 에러, 그 밖(inactive)은 정상 종료로 본다
                self._set_state(ProcessState.ERROR if active == "failed"
                                else ProcessState.IDLE)
                self._stop_log_stream()
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

    def recent_log(self, lines: int = 20) -> list[str]:
        """이 유닛의 저널 마지막 몇 줄. **죽은 이유를 보여주는 유일한 단서다.**

        ⚠ `start()` 가 `--collect` 로 띄우므로 유닛은 **끝나는 즉시 사라진다.**
        그래서 `is-active` 는 실패한 종료도 `inactive` 로 답하고, 우리 상태는
        정상 종료(IDLE)로 읽는다 — 화면에는 "그냥 꺼졌다"만 뜬다. 종료 코드도
        `Result` 도 남지 않으므로, 사후에 남는 것은 저널뿐이다.

        ⚠ 유닛 이름으로 읽으므로 **지난 실행의 줄이 섞일 수 있다.** 그래서
        이 게이트웨이가 띄운 적이 있을 때만 돌려준다 — 그 전 것은 남의 기록이다.
        """
        if not self._started_once:
            return []
        try:
            out = subprocess.run(
                ["journalctl", "--user", "-u", self.unit, "--lines", str(lines), "-o", "cat"],
                capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [ln for ln in out.splitlines() if ln.strip()]

    def _set_state(self, state: ProcessState) -> None:
        self._state = state
        if self._on_state:
            self._on_state(state)

    def set_log_callback(self, cb: Callable[[str], None]) -> None:
        self._on_log = cb

    def set_state_callback(self, cb: Callable[[ProcessState], None]) -> None:
        self._on_state = cb

    # ── 실행 ──

    async def start(self, cmd: list[str], env_extra: dict[str, str] | None = None) -> None:
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
        # ⚠ **`--setenv` 는 `systemd-run` 인자다 — `--` 앞에 와야 한다.**
        # `cmd`(실행할 명령)에 쌓으면 학습 스크립트가 받아 파싱하다 죽는다:
        #   `lerobot_train.py: error: unrecognized arguments: --setenv=...`
        # 실기에서 이렇게 터졌다.
        #
        # ⚠ 유닛은 **게이트웨이의 환경을 물려받지 않는다** — 사용자 systemd 의
        # 환경으로 뜬다. 버스 주소·HF 엔드포인트처럼 우리가 넣어주는 것은
        # `injected_env()` 가 한 벌로 갖고 있으므로 자식 소유자가 둘이어도 같다.
        for k, v in injected_env(env_extra).items():
            argv += [f"--setenv={k}={v}"]
        argv += ["--"] + list(cmd)

        self._set_state(ProcessState.STARTING)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            self._set_state(ProcessState.ERROR)
            raise RuntimeError(f"유닛 시작 실패: {(out or b'').decode(errors='replace')}")

        self._started_once = True
        self._set_state(ProcessState.RUNNING)
        self._start_log_stream()
        logger.info("유닛 시작: %s", self.unit)

    async def stop(self) -> None:
        _systemctl("stop", self.unit)
        _systemctl("reset-failed", self.unit)
        self._stop_log_stream()
        self._set_state(ProcessState.IDLE)

    # ── 로그 ──

    def _start_log_stream(self, follow_from_start: bool = False) -> None:
        """journald 를 따라 읽어 콜백으로 넘긴다.

        `follow_from_start` 는 **재부착할 때** 쓴다 — 게이트웨이가 없던 동안의
        로그를 처음부터 다시 읽어야 화면의 진행률이 이어진다.

        ⚠ **스레드로 돈다. asyncio 가 아니다.** `restore()` 는 `TrainRunner`
        프로토콜상 **동기** 메서드라 이벤트 루프 밖에서 불릴 수 있는데,
        `asyncio.create_task` 는 그때 `RuntimeError: no running event loop` 로
        죽는다 — 실기에서 걸렸다. 콜백도 동기 함수라 루프를 요구할 이유가 없다.
        """
        if self._log_thread and self._log_thread.is_alive():
            return

        def _pump() -> None:
            args = ["journalctl", "--user", "-f", "-o", "cat"]
            if follow_from_start:
                # ⚠ **이번 실행분만 읽는다.** journald 는 같은 유닛 이름의 로그를
                # 실행이 바뀌어도 계속 쌓는다. `-u <unit> --lines=all` 로 읽으면
                # **지난 학습의 스텝이 진행률로 들어온다** — 실기에서 확인했다.
                # 실행마다 바뀌는 InvocationID 로 이번 것만 고른다.
                inv = _systemctl("show", self.unit,
                                 "--property=InvocationID").stdout.strip()
                inv = inv.split("=", 1)[-1] if "=" in inv else ""
                if inv:
                    # `--lines=all` 이 없으면 `-f` 가 **마지막 몇 줄만** 보여준다 —
                    # 재부착의 요점이 처음부터 다시 읽는 것이므로 반드시 준다.
                    args += ["--lines=all", f"_SYSTEMD_INVOCATION_ID={inv}"]
                else:
                    logger.warning("InvocationID 를 못 읽어 이번 실행분만 고를 수 없다 (%s)",
                                   self.unit)
                    args += ["-u", self.unit, "--lines=all"]
            else:
                args += ["-u", self.unit, "--lines=0"]
            try:
                proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
            except Exception as exc:
                logger.warning("journald 연결 실패 (%s): %s", self.unit, exc)
                return
            self._log_proc = proc
            try:
                for raw in proc.stdout or ():
                    if self._log_stop.is_set():
                        break
                    line = raw.rstrip("\n")
                    if line and self._on_log:
                        self._on_log(line)
            finally:
                with contextlib.suppress(Exception):
                    proc.terminate()

        self._log_stop.clear()
        self._log_thread = threading.Thread(target=_pump, daemon=True,
                                            name=f"journal-{self.unit}")
        self._log_thread.start()

    def _stop_log_stream(self) -> None:
        self._log_stop.set()
        with contextlib.suppress(Exception):
            if self._log_proc:
                self._log_proc.terminate()
        self._log_proc = None
        self._log_thread = None

    def exec_argv(self) -> list[str]:
        """유닛이 실제로 돌리는 명령. **재부착 때 인자를 되찾는 유일한 경로다.**

        journald 로그에서 긁을 수도 있지만 그건 출력 형식에 기대는 것이고,
        `ExecStart` 는 systemd 가 들고 있는 사실이다.
        """
        out = _systemctl("show", self.unit, "--property=ExecStart").stdout
        # `{ path=... ; argv[]=python -m ... ; ... }` 형태에서 argv 만 꺼낸다
        if "argv[]=" not in out:
            return []
        tail = out.split("argv[]=", 1)[1]
        return tail.split(" ; ", 1)[0].strip().split()

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
