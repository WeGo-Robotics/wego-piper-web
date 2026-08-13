"""systemd 러너 — 학습을 **게이트웨이 밖의 유닛**으로 띄운다 (ROADMAP 3b-6).

`LocalRunner` 는 subprocess 를 자식으로 들고 있어서, 게이트웨이가 죽으면
학습은 계속 도는데 화면에서 사라진다. PID 파일로 되살리지만 그건 재부팅에
날아가고 PID 재사용 위험도 있으며, **stdout 은 영영 못 되돌린다.**

systemd 에 맡기면 셋이 한꺼번에 풀린다:

- 유닛이 소유자다. 게이트웨이 재시작·크래시와 무관하게 산다
- 상태는 `systemctl is-active` 가 답한다. PID 파일이 필요 없다
- 로그는 journald 에 남는다 — **재부착하면 이어서 읽는다.** 이게 핵심이다

## `--user` 를 쓴다

시스템 유닛으로 띄우면 학습이 root 로 돌고 산출물 소유자가 어긋난다.
사용자 유닛이면 데이터 루트 권한이 지금 그대로다.

⚠ 사용자 유닛은 **로그아웃하면 함께 죽는다.** 이 프로젝트는 이미 그걸 겪었고
(`loginctl enable-linger` 로 해결), 그 전제 위에서만 이 러너가 의미가 있다.
linger 가 꺼져 있으면 `LocalRunner` 와 다를 게 없다 — 그래서 기동 시 확인한다.
"""

import asyncio
import contextlib
import logging
import shutil
import subprocess
from collections.abc import Callable

from app.services.process_manager import ProcessState
from app.services.training.spec import TrainJobSpec

logger = logging.getLogger(__name__)

# 유닛 이름. `piper-train-` 접두사로 우리 것만 골라낸다 — 재부착할 때
# 남의 유닛을 건드리지 않기 위해서다.
UNIT_PREFIX = "piper-train-"


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


class SystemdRunner:
    """`TrainRunner` 구현. 인터페이스는 `LocalRunner` 와 같다."""

    def __init__(self, job_id: str = "local") -> None:
        self.unit = f"{UNIT_PREFIX}{job_id}"
        self._state = ProcessState.IDLE
        self._on_log: Callable[[str], None] | None = None
        self._on_state: Callable[[ProcessState], None] | None = None
        self._log_task: asyncio.Task | None = None
        self._spec: TrainJobSpec | None = None

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

    async def start(self, spec: TrainJobSpec) -> None:
        ok, why = available()
        if not ok:
            raise RuntimeError(f"systemd 러너를 쓸 수 없습니다: {why}")
        # 지난 실행이 실패로 남아 있으면 같은 이름으로 못 띄운다
        _systemctl("reset-failed", self.unit)

        cmd = [
            "systemd-run", "--user", f"--unit={self.unit}",
            "--property=Type=exec",
            # ⚠ 게이트웨이가 죽어도 유닛은 살아야 한다 — 그게 이 러너의 존재 이유다.
            #   `--scope` 를 쓰면 호출자의 cgroup 에 들어가 함께 죽는다.
            "--collect",
        ]
        for k, v in (spec.env or {}).items():
            cmd += [f"--setenv={k}={v}"]
        cmd += ["--"] + list(spec.cmd)

        self._set_state(ProcessState.STARTING)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            self._set_state(ProcessState.ERROR)
            raise RuntimeError(f"유닛 시작 실패: {(out or b'').decode(errors='replace')}")

        self._spec = spec
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

    def restore(self) -> TrainJobSpec | None:
        """게이트웨이 재시작 후 살아있는 유닛에 **다시 붙는다.**

        PID 파일을 보지 않는다 — systemd 가 진실이다. 로그도 journald 에서
        처음부터 다시 읽으므로, 없던 동안의 진행률이 화면에 채워진다.
        """
        if _systemctl("is-active", self.unit).stdout.strip() != "active":
            return None
        self._set_state(ProcessState.RUNNING)
        self._start_log_stream(follow_from_start=True)
        logger.info("학습 유닛 재부착: %s", self.unit)
        # cmd/총 스텝은 로그를 다시 읽으며 파서가 채운다 — 여기서 지어내지 않는다.
        return TrainJobSpec(cmd=[], total_steps=0, output_dir="")

