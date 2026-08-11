"""학습 관리 — 러너(실행 방식)와 메트릭(파싱)을 조합한다.

이전에는 `TrainManager` 가 곧 `ProcessManager` 였다 — `state`/`is_running`/`stop` 이
전부 로컬 subprocess 에 위임됐고, 원격 job 에는 PID 도 SIGTERM 도 stdout 파이프도 없다
(feature/cloud-training.md §1-(1)).

이제 `TrainRunner` 이음매만 보므로 `SSHRunner`(클라우드) 나
`SystemdRunner`(데몬 분리) 를 나란히 붙일 수 있다.
"""

import logging
from collections.abc import Callable

from app.services.process_manager import ProcessState
from app.services.training.metrics import MetricsTracker
from app.services.training.runners.base import TrainRunner
from app.services.training.runners.local import LocalRunner
from app.services.training.spec import TrainJobSpec

logger = logging.getLogger(__name__)


class TrainManager:
    def __init__(self, runner: TrainRunner | None = None) -> None:
        self.runner: TrainRunner = runner or LocalRunner()
        self.tracker = MetricsTracker()
        self.output_dir: str = ""
        self._on_metrics: Callable[[dict], None] | None = None
        self._original_log_cb: Callable[[str], None] | None = None
        self.tracker.set_update_callback(self._emit_metrics)

    # ── 상태 (실행 방식 무관) ──

    @property
    def state(self) -> ProcessState:
        return self.runner.state

    @property
    def is_running(self) -> bool:
        return self.runner.is_running

    @property
    def metrics(self):
        return self.tracker.metrics

    @property
    def history(self):
        return self.tracker.history

    # ── 콜백 ──

    def set_metrics_callback(self, cb: Callable[[dict], None]) -> None:
        self._on_metrics = cb

    def set_log_callback(self, cb: Callable[[str], None]) -> None:
        self._original_log_cb = cb
        self.runner.set_log_callback(self._intercept_log)

    def set_state_callback(self, cb: Callable[[ProcessState], None]) -> None:
        self.runner.set_state_callback(cb)

    def _intercept_log(self, line: str) -> None:
        """로그를 가로채 메트릭을 뽑고 원본 콜백에 그대로 전달."""
        self.tracker.feed(line)
        if self._original_log_cb:
            self._original_log_cb(line)

    def _emit_metrics(self) -> None:
        if self._on_metrics:
            self._on_metrics(self.get_status())

    # ── 실행 ──

    async def start(
        self,
        cmd: list[str],
        total_steps: int = 0,
        output_dir: str = "",
        env_extra: dict[str, str] | None = None,
    ) -> None:
        self.tracker.reset(total_steps=total_steps)
        self.output_dir = output_dir
        await self.runner.start(
            TrainJobSpec(
                cmd=cmd,
                total_steps=total_steps,
                output_dir=output_dir,
                env=env_extra or {},
            )
        )

    async def stop(self) -> None:
        await self.runner.stop()

    def restore_running_process(self) -> bool:
        spec = self.runner.restore()
        if spec is None:
            return False
        self.tracker.reset(total_steps=spec.total_steps)
        self.output_dir = spec.output_dir
        return True

    def get_status(self) -> dict:
        m = self.tracker.metrics
        return {
            "state": self.state.value,
            "step": m.step,
            "total_steps": m.total_steps,
            "progress": self.tracker.progress(),
            "loss": m.loss,
            "grad_norm": m.grad_norm,
            "lr": m.lr,
            "epoch": m.epoch,
            "update_s": m.update_time,  # GPU 갱신 시간 (병목 힌트)
            "data_s": m.data_time,      # 데이터 로딩 시간 (병목 힌트)
            "output_dir": self.output_dir,
        }


train_manager = TrainManager()
