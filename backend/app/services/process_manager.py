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
            # subprocess에 깨끗한 환경 전달 (PYTHONPATH 오염 방지).
            # 단, logfix 디렉토리만 PYTHONPATH에 넣어 sitecustomize.py가
            # 자동 import되도록 한다 (라이브러리 로거 이중 출력 차단).
            import os as _os
            from pathlib import Path as _Path
            env = _os.environ.copy()
            _logfix_dir = str(_Path(__file__).resolve().parent.parent / "core" / "logfix")
            env["PYTHONPATH"] = _logfix_dir
            # pynput이 X server에 연결할 수 있도록 DISPLAY 보장
            if "DISPLAY" not in env:
                env["DISPLAY"] = ":0"
            # LeRobot 내부가 huggingface_hub 를 직접 부른다 — 우리 코드를 안 거치므로
            # 자체 Hub 로 방향을 바꾸는 레버는 이 환경변수 하나뿐이다.
            from app.core.config import settings as _settings
            if _settings.hf_endpoint:
                env["HF_ENDPOINT"] = _settings.hf_endpoint
            # 호출부 지정 추가 환경변수 (예: ACCELERATE_MIXED_PRECISION) 주입
            if env_extra:
                env.update(env_extra)

            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._set_state(ProcessState.RUNNING)
            self._log_task = asyncio.create_task(self._read_stdout())
            self._err_task = asyncio.create_task(self._read_stderr())
        except Exception as e:
            logger.error("Failed to start process: %s", e)
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
