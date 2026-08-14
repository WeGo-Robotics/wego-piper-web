"""로컬 subprocess 러너 — 기존 `TrainManager` 의 실행 부분을 그대로 옮긴 것.

동작 변화 없음. PID 파일 기반 복원도 그대로다.

> ⚠ `/tmp/piper_train_pid` 는 재부팅 시 날아가고 PID 재사용 위험도 있다.
> 원격 job 에는 아예 적용할 수 없다 — 클라우드 러너가 붙을 때
> 영속 job 레지스트리로 대체된다 (feature/cloud-training.md §1-(3), 3단계).
"""

import logging
import os
from collections.abc import Callable

from app.services.process_manager import ProcessManager, ProcessState
from app.services.training.spec import TrainJobSpec

logger = logging.getLogger(__name__)

_PID_FILE = "/tmp/piper_train_pid"  # nosec B108


class LocalRunner:
    # 이 기계의 GPU 를 쓴다.
    occupies_local_gpu = True

    def __init__(self) -> None:
        self.pm = ProcessManager()

    @property
    def state(self) -> ProcessState:
        return self.pm.state

    @property
    def is_running(self) -> bool:
        return self.pm.state in (ProcessState.RUNNING, ProcessState.STARTING)

    @property
    def pid(self) -> int | None:
        return self.pm.pid

    def set_log_callback(self, cb: Callable[[str], None]) -> None:
        self.pm.set_log_callback(cb)

    def set_state_callback(self, cb: Callable[[ProcessState], None]) -> None:
        def _wrap(state: ProcessState) -> None:
            if state in (ProcessState.IDLE, ProcessState.ERROR):
                self._remove_pid_file()
            cb(state)

        self.pm.set_state_callback(_wrap)

    async def start(self, spec: TrainJobSpec) -> None:
        await self.pm.start(spec.cmd, env_extra=spec.env or None)
        if self.pm.pid:
            try:
                with open(_PID_FILE, "w") as f:
                    f.write(f"{self.pm.pid}\n{spec.total_steps}\n{spec.output_dir}")
            except Exception:
                pass

    async def stop(self) -> None:
        await self.pm.stop()
        self._remove_pid_file()

    def _remove_pid_file(self) -> None:
        try:
            os.remove(_PID_FILE)
        except FileNotFoundError:
            pass

    def restore(self) -> TrainJobSpec | None:
        """서버 재시작 시 실행 중인 학습 프로세스 복원.

        stdout 재연결은 불가하다 — 상태와 진행률만 되살린다.
        """
        try:
            if not os.path.exists(_PID_FILE):
                return None
            with open(_PID_FILE) as f:
                lines = f.read().strip().split("\n")
            pid = int(lines[0])
            total_steps = int(lines[1]) if len(lines) > 1 else 0
            output_dir = lines[2] if len(lines) > 2 else ""
            os.kill(pid, 0)  # 살아있는지 확인
            self.pm._state = ProcessState.RUNNING
            if self.pm._on_state_change:
                self.pm._on_state_change(ProcessState.RUNNING)
            logger.info("Restored training process: pid=%d, steps=%d", pid, total_steps)
            return TrainJobSpec(cmd=[], total_steps=total_steps, output_dir=output_dir)
        except (ProcessLookupError, ValueError, FileNotFoundError):
            self._remove_pid_file()
            return None
