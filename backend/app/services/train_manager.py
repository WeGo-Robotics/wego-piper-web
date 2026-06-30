"""
학습 프로세스 관리.
ProcessManager를 재활용하되, 학습 메트릭 파싱 + 히스토리 + GPU 경합 방지.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from app.services.process_manager import ProcessManager, ProcessState

logger = logging.getLogger(__name__)

# step:200 smpl:12.8K ep:106 epch:1.07 loss:0.342 grdn:2.157 lr:1.0e-03 updt_s:0.456 data_s:0.012
# step:1K smpl:64K ep:154 epch:3.07 ...  (1000스텝 이후 K 접미사)
def _parse_num(s: str) -> int:
    """'34K' → 34000, '1.2M' → 1200000, '200' → 200"""
    s = s.strip()
    if s.endswith('K'):
        return int(float(s[:-1]) * 1000)
    elif s.endswith('M'):
        return int(float(s[:-1]) * 1000000)
    return int(s)


_METRIC_RE = re.compile(
    r"step:([\d.]+[KM]?)\s+smpl:([\d.]+[KM]?)\s+ep:([\d.]+[KM]?)\s+epch:([\d.]+)\s+"
    r"loss:([\d.]+)\s+grdn:([\d.]+)\s+lr:([\d.e+-]+)\s+"
    r"updt_s:([\d.]+)\s+data_s:([\d.]+)"
)


@dataclass
class TrainMetrics:
    step: int = 0
    total_steps: int = 0
    loss: float = 0.0
    grad_norm: float = 0.0
    lr: float = 0.0
    epoch: float = 0.0
    update_time: float = 0.0
    data_time: float = 0.0


@dataclass
class TrainHistory:
    steps: list[int] = field(default_factory=list)
    losses: list[float] = field(default_factory=list)
    grad_norms: list[float] = field(default_factory=list)
    lrs: list[float] = field(default_factory=list)
    max_points: int = 5000  # 메모리 제한

    def append(self, step: int, loss: float, grad_norm: float, lr: float) -> None:
        if len(self.steps) >= self.max_points:
            # 오래된 절반 버리기
            half = self.max_points // 2
            self.steps = self.steps[half:]
            self.losses = self.losses[half:]
            self.grad_norms = self.grad_norms[half:]
            self.lrs = self.lrs[half:]
        self.steps.append(step)
        self.losses.append(loss)
        self.grad_norms.append(grad_norm)
        self.lrs.append(lr)

    def to_dict(self) -> dict:
        return {
            "steps": self.steps,
            "losses": self.losses,
            "grad_norms": self.grad_norms,
            "lrs": self.lrs,
        }

    def clear(self) -> None:
        self.steps.clear()
        self.losses.clear()
        self.grad_norms.clear()
        self.lrs.clear()


_PID_FILE = "/tmp/piper_train_pid"


class TrainManager:
    def __init__(self) -> None:
        self.pm = ProcessManager()
        self.metrics = TrainMetrics()
        self.history = TrainHistory()
        self.output_dir: str = ""
        self._on_metrics: Callable[[dict], None] | None = None
        self._original_log_cb: Callable[[str], None] | None = None

    @property
    def state(self) -> ProcessState:
        return self.pm.state

    @property
    def is_running(self) -> bool:
        return self.pm.state in (ProcessState.RUNNING, ProcessState.STARTING)

    def set_metrics_callback(self, cb: Callable[[dict], None]) -> None:
        self._on_metrics = cb

    def set_log_callback(self, cb: Callable[[str], None]) -> None:
        self._original_log_cb = cb
        self.pm.set_log_callback(self._intercept_log)

    def set_state_callback(self, cb: Callable[[ProcessState], None]) -> None:
        def _wrap(state: ProcessState) -> None:
            if state in (ProcessState.IDLE, ProcessState.ERROR):
                self._remove_pid_file()
            cb(state)
        self.pm.set_state_callback(_wrap)

    def _intercept_log(self, line: str) -> None:
        """로그 가로채서 메트릭 파싱 후 원본 콜백에 전달."""
        self._try_parse_metrics(line)
        if self._original_log_cb:
            self._original_log_cb(line)

    def _try_parse_metrics(self, line: str) -> None:
        m = _METRIC_RE.search(line)
        if not m:
            return
        self.metrics.step = _parse_num(m.group(1))
        self.metrics.epoch = float(m.group(4))
        self.metrics.loss = float(m.group(5))
        self.metrics.grad_norm = float(m.group(6))
        self.metrics.lr = float(m.group(7))
        self.metrics.update_time = float(m.group(8))
        self.metrics.data_time = float(m.group(9))

        self.history.append(
            self.metrics.step, self.metrics.loss,
            self.metrics.grad_norm, self.metrics.lr,
        )

        if self._on_metrics:
            self._on_metrics(self.get_status())

    async def start(
        self,
        cmd: list[str],
        total_steps: int = 0,
        output_dir: str = "",
        env_extra: dict[str, str] | None = None,
    ) -> None:
        self.metrics = TrainMetrics(total_steps=total_steps)
        self.history.clear()
        self.output_dir = output_dir
        await self.pm.start(cmd, env_extra=env_extra)
        # PID 저장
        if self.pm.pid:
            try:
                with open(_PID_FILE, "w") as f:
                    f.write(f"{self.pm.pid}\n{total_steps}\n{output_dir}")
            except Exception:
                pass

    async def stop(self) -> None:
        await self.pm.stop()
        self._remove_pid_file()

    def _remove_pid_file(self) -> None:
        import os
        try:
            os.remove(_PID_FILE)
        except FileNotFoundError:
            pass

    def restore_running_process(self) -> bool:
        """서버 재시작 시 실행 중인 학습 프로세스 복원."""
        import os
        try:
            if not os.path.exists(_PID_FILE):
                return False
            with open(_PID_FILE) as f:
                lines = f.read().strip().split("\n")
            pid = int(lines[0])
            total_steps = int(lines[1]) if len(lines) > 1 else 0
            output_dir = lines[2] if len(lines) > 2 else ""
            # 프로세스가 살아있는지 확인
            os.kill(pid, 0)
            # 살아있으면 상태 복원 (stdout 재연결은 불가하지만 상태는 표시)
            self.metrics = TrainMetrics(total_steps=total_steps)
            self.output_dir = output_dir
            self.pm._state = ProcessState.RUNNING
            if self.pm._on_state_change:
                self.pm._on_state_change(ProcessState.RUNNING)
            logger.info("Restored training process: pid=%d, steps=%d", pid, total_steps)
            return True
        except (ProcessLookupError, ValueError, FileNotFoundError):
            self._remove_pid_file()
            return False

    def get_status(self) -> dict:
        progress = 0.0
        if self.metrics.total_steps > 0 and self.metrics.step > 0:
            progress = self.metrics.step / self.metrics.total_steps
        return {
            "state": self.pm.state.value,
            "step": self.metrics.step,
            "total_steps": self.metrics.total_steps,
            "progress": round(progress, 4),
            "loss": self.metrics.loss,
            "grad_norm": self.metrics.grad_norm,
            "lr": self.metrics.lr,
            "epoch": self.metrics.epoch,
            "update_s": self.metrics.update_time,  # GPU 갱신 시간 (병목 힌트)
            "data_s": self.metrics.data_time,      # 데이터 로딩 시간 (병목 힌트)
            "output_dir": self.output_dir,
        }


train_manager = TrainManager()
