"""
LeRobot 프로세스 생명주기 관리.
subprocess.Popen으로 CLI를 실행하고 stdout/stderr를 실시간 파싱.
"""

import asyncio
import enum
import logging
import signal
from collections.abc import Callable

logger = logging.getLogger(__name__)


class ProcessState(str, enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


def injected_env(env_extra: dict[str, str] | None = None) -> dict[str, str]:
    """자식이 **우리 것이라서** 받아야 하는 환경변수. 상속분 위에 얹는다.

    ⚠ 소유자가 둘이라 여기 모은다. `ProcessManager` 는 `os.environ` 사본에
    update 하고, `SystemdProcess` 는 같은 dict 를 `--setenv` 로 넘긴다.
    `ProcessManager` 안에만 두면 **유닛으로 띄운 자식만 조용히 이걸 못 받는다** —
    버스 주소가 그중 하나라, 못 받은 채널이 조용히 죽는 게 정확히 아래 3단계에서
    겪은 실패다.

    `os.environ` 전체를 반환하지 않는다. 유닛에 `--setenv` 로 수백 개를 넘기는
    꼴이 되고, 상속으로 이미 오는 것을 덮어쓸 이유도 없다.
    """
    import os as _os
    from pathlib import Path as _Path

    env: dict[str, str] = {}
    # logfix 디렉토리를 PYTHONPATH 에 넣어 sitecustomize.py 가 자동 import 되게 한다
    # (라이브러리 로거 이중 출력 차단). 덮어쓰는 것은 PYTHONPATH 오염 방지도 겸한다.
    env["PYTHONPATH"] = str(_Path(__file__).resolve().parent.parent / "core" / "logfix")
    # pynput 이 X server 에 연결할 수 있도록 DISPLAY 보장
    env["DISPLAY"] = _os.environ.get("DISPLAY") or ":0"
    # LeRobot 내부가 huggingface_hub 를 직접 부른다 — 우리 코드를 안 거치므로
    # 자체 Hub 로 방향을 바꾸는 레버는 이 환경변수 하나뿐이다.
    from app.core.config import settings as _settings
    if _settings.hf_endpoint:
        env["HF_ENDPOINT"] = _settings.hf_endpoint
    # 버스 주소는 **여기 한 곳에서** 모든 자식에게 준다. 예전에는 ZMQ 주소 3개를
    # 호출부마다 따로 넘겼는데, 하나를 빠뜨리면 그 채널만 조용히 죽었다
    # (refactor/daemon-split.md 3단계). 버스를 안 쓰는 자식은 그냥 무시한다.
    from piper_bus.client import url as _bus_url
    env["PIPER_REDIS_URL"] = _bus_url()
    # 호출부 지정 추가 환경변수 (예: ACCELERATE_MIXED_PRECISION)
    if env_extra:
        env.update(env_extra)
    return env


class ProcessManager:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._state = ProcessState.IDLE
        self._on_log: Callable[[str], None] | None = None
        self._on_state_change: Callable[[ProcessState], None] | None = None
        self._log_task: asyncio.Task | None = None
        self._current_cmd: list[str] = []

    @property
    def state(self) -> ProcessState:
        return self._state

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def set_log_callback(self, cb: Callable[[str], None]) -> None:
        self._on_log = cb

    def set_state_callback(self, cb: Callable[[ProcessState], None]) -> None:
        self._on_state_change = cb

    def ensure_log_reader(self) -> None:
        """stdout reader가 죽었으면 현재 이벤트 루프에서 재시작."""
        if (
            self._process
            and self._process.returncode is None
            and self._state == ProcessState.RUNNING
            and (self._log_task is None or self._log_task.done())
        ):
            logger.info("Restarting stdout reader for pid=%s", self._process.pid)
            self._log_task = asyncio.create_task(self._read_stdout())

    def _set_state(self, state: ProcessState) -> None:
        self._state = state
        if self._on_state_change:
            self._on_state_change(state)

    async def start(self, cmd: list[str], env_extra: dict[str, str] | None = None) -> None:
        """CLI 명령어를 subprocess로 실행."""
        if self._state not in (ProcessState.IDLE, ProcessState.ERROR):
            raise RuntimeError(f"Cannot start: current state is {self._state}")

        self._current_cmd = cmd
        self._set_state(ProcessState.STARTING)
        logger.info("Starting process: %s", " ".join(cmd))

        try:
            import os as _os
            env = _os.environ.copy()
            env.update(injected_env(env_extra))

            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._set_state(ProcessState.RUNNING)
            self._log_task = asyncio.create_task(self._read_stdout())
            self._err_task = asyncio.create_task(self._read_stderr())
        except FileNotFoundError as e:
            # ⚠ `[Errno 2] No such file or directory` 는 **어느 파일인지 안 말한다.**
            #   실행 파일이 없는 것인데 모델 경로 문제로 읽혀서 한참을 헤맸다
            #   (`local_python` 이 PATH 에 없던 건). 이름을 찍는다.
            logger.error("실행 파일을 못 찾음: %s (%s)", cmd[0], e)
            self._set_state(ProcessState.ERROR)
            raise FileNotFoundError(f"실행 파일이 없습니다: {cmd[0]}") from e
        except Exception as e:
            logger.error("Failed to start process: %s (cmd=%s)", e, cmd[0])
            self._set_state(ProcessState.ERROR)
            raise

    async def _read_stderr(self) -> None:
        """stderr를 읽어서 로그 콜백에 전달. ws.py에서 파싱."""
        if not self._process or not self._process.stderr:
            return
        try:
            async for line_bytes in self._process.stderr:
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                if line and self._on_log:
                    self._on_log(line)
        except asyncio.CancelledError:
            pass

    async def _read_stdout(self) -> None:
        """stdout을 한 줄씩 읽어 콜백으로 전달."""
        assert self._process and self._process.stdout
        try:
            async for line_bytes in self._process.stdout:
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                if self._on_log:
                    self._on_log(line)
            # 프로세스 종료 대기
            await self._process.wait()
            code = self._process.returncode
            if self._state == ProcessState.STOPPING:
                self._set_state(ProcessState.IDLE)
            elif code == 0:
                self._set_state(ProcessState.IDLE)
            else:
                logger.warning("Process exited with code %d", code)
                self._set_state(ProcessState.ERROR)
        except asyncio.CancelledError:
            pass

    async def stop(self, timeout: float = 15.0) -> None:
        """Graceful shutdown: SIGTERM → timeout → SIGKILL."""
        if not self._process or self._state not in (
            ProcessState.RUNNING,
            ProcessState.STARTING,
        ):
            return

        self._set_state(ProcessState.STOPPING)
        logger.info("Stopping process (pid=%s)", self._process.pid)

        try:
            self._process.send_signal(signal.SIGTERM)
            await asyncio.wait_for(self._process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("SIGTERM timeout, sending SIGKILL")
            self._process.kill()
            await self._process.wait()

        if self._log_task:
            self._log_task.cancel()
            self._log_task = None

        self._set_state(ProcessState.IDLE)

    async def kill(self) -> None:
        """즉시 강제 종료 (E-stop용)."""
        if self._process and self._process.returncode is None:
            logger.warning("Force killing process (pid=%s)", self._process.pid)
            self._process.kill()
            await self._process.wait()
            if self._log_task:
                self._log_task.cancel()
                self._log_task = None
            self._set_state(ProcessState.IDLE)


# 싱글톤 인스턴스
process_manager = ProcessManager()
