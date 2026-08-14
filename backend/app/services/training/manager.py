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
from app.services.training.jobs import (
    LOCAL_JOB_ID,
    JobRecord,
    JobRegistry,
    job_registry,
)
from app.services.training.metrics import MetricsTracker
from app.services.training.runners.base import TrainRunner
from app.services.training.runners.local import LocalRunner

from app.services.training.spec import TrainJobSpec

logger = logging.getLogger(__name__)


def _default_runner() -> "TrainRunner":
    """설정이 고른 러너. **못 쓰면 조용히 떨어지지 않고 말한다.**

    조용히 `LocalRunner` 로 폴백하면 "재시작해도 학습이 살아있다"고 믿는데
    실제로는 아닌 상태가 된다 — 그게 가장 나쁜 결과다.

    원격이 로컬보다 먼저다. 둘 다 설정돼 있으면 원격이 이긴다 — `process_runner`
    는 "이 기계에서 어떻게 띄우나"이고 `train_ssh_host` 는 "어느 기계에서 도나"라
    층이 다르다.
    """
    from app.core.config import settings

    if settings.train_ssh_host:
        from app.services.training.runners.ssh import SSHRunner
        from app.services.training.runners.ssh import available as ssh_available

        ok, why = ssh_available()
        if ok:
            return SSHRunner()
        # ⚠ 여기서 조용히 로컬로 떨어지면 **이 기계의 GPU 를 먹는다.** 원격에서
        #   돈다고 믿고 추론을 같이 걸면 OOM 이다 — 그래서 크게 남긴다.
        logger.warning("원격 학습 러너를 쓸 수 없어 local 로 돕니다 — %s "
                       "(학습이 이 기계의 GPU 를 씁니다)", why)
        return LocalRunner()

    if settings.process_runner != "systemd":
        return LocalRunner()

    from app.services.training.runners.systemd import SystemdRunner, available

    ok, why = available()
    if not ok:
        logger.warning("systemd 러너를 쓸 수 없어 local 로 돕니다 — %s "
                       "(재시작하면 학습이 화면에서 사라집니다)", why)
        return LocalRunner()
    return SystemdRunner()


class TrainManager:
    def __init__(
        self,
        runner: TrainRunner | None = None,
        job_id: str = LOCAL_JOB_ID,
        registry: JobRegistry | None = None,
    ) -> None:
        self.runner: TrainRunner = runner or _default_runner()
        self.tracker = MetricsTracker()
        self.output_dir: str = ""
        # 로컬도 job_id 를 갖는다 — 원격이 붙어도 같은 경로를 타게 하려는 것이다
        # (feature/cloud-training.md 3단계).
        self.job_id = job_id
        self.registry = registry or job_registry
        self._on_metrics: Callable[[dict], None] | None = None
        self._original_log_cb: Callable[[str], None] | None = None
        self._original_state_cb: Callable[[ProcessState], None] | None = None
        self.tracker.set_update_callback(self._emit_metrics)

    # ── 상태 (실행 방식 무관) ──

    @property
    def state(self) -> ProcessState:
        return self.runner.state

    @property
    def is_running(self) -> bool:
        return self.runner.is_running

    @property
    def uses_local_gpu(self) -> bool:
        """이 학습이 **이 기계의** GPU 를 쓰는가. 배타 가드가 본다.

        기본은 True 다 — 모르면 막는 쪽이 안전하다. 원격 러너만 False 로 답한다.
        """
        return getattr(self.runner, "occupies_local_gpu", True)

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
        self._original_state_cb = cb
        self.runner.set_state_callback(self._intercept_state)

    def _intercept_log(self, line: str) -> None:
        """로그를 가로채 메트릭을 뽑고 원본 콜백에 그대로 전달."""
        self.tracker.feed(line)
        # 6시간 학습이면 수만 줄이라 전부 WS 로 밀면 브라우저가 죽는다.
        # 버스 링버퍼에 남겨 REST 페이지네이션으로 읽게 한다 (WS 로는 신규분만).
        self.registry.append_log(self.job_id, line)
        if self._original_log_cb:
            self._original_log_cb(line)

    def _intercept_state(self, state: ProcessState) -> None:
        """상태 변화를 레지스트리에 반영하고 원본 콜백에 전달.

        여기서 쓰지 않으면 **게이트웨이가 재시작할 때 학습 상태가 사라진다** —
        지금까지의 그 버그다. 레지스트리가 프로세스 밖에 있으니 다시 읽으면 그만이다.
        """
        self._sync_record(state=state.value)
        if self._original_state_cb:
            self._original_state_cb(state)

    def _emit_metrics(self) -> None:
        status = self.get_status()
        self._sync_record(metrics=status)
        if self._on_metrics:
            self._on_metrics(status)

    def _sync_record(self, state: str | None = None, metrics: dict | None = None) -> None:
        record = self.registry.get(self.job_id) or JobRecord(job_id=self.job_id)
        record.runner = type(self.runner).__name__.replace("Runner", "").lower()
        record.state = state if state is not None else self.state.value
        # ⚠ **빈 값으로 덮어쓰지 않는다.** 서버 재시작 직후엔 트래커가 비어 있는데,
        # 그대로 쓰면 살아남은 레코드의 `total_steps`/`output_dir` 이 0과 ""가 된다 —
        # 레지스트리를 둔 이유(재시작해도 학습이 보인다)를 스스로 깎는다.
        if self.output_dir:
            record.output_dir = self.output_dir
        if self.tracker.metrics.total_steps:
            record.total_steps = self.tracker.metrics.total_steps
        if metrics is not None:
            record.metrics = metrics
        self.registry.put(record)

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
        # 이전 job 의 로그·레코드를 치운다. 남겨두면 새 학습 로그가 옛 줄 뒤에 붙어
        # "어디부터 이번 학습인가"를 알 수 없다 — 버스 링버퍼는 세션을 넘어 살아남는다.
        self.registry.delete(self.job_id)
        self._sync_record(state=ProcessState.STARTING.value)
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
        """서버 재시작 후 살아있는 학습에 다시 붙는다.

        레지스트리도 함께 맞춘다. 안 맞추면 프로세스는 죽었는데 레코드만
        `running` 으로 남아 UI 가 영원히 "학습 중"이라고 말한다.
        """
        spec = self.runner.restore()
        if spec is None:
            stale = self.registry.get(self.job_id)
            if stale and stale.is_active:
                logger.info("죽은 학습 job 레코드를 정리한다: %s", self.job_id)
                self._sync_record(state=ProcessState.IDLE.value)
            return False
        self.tracker.reset(total_steps=spec.total_steps)
        self.output_dir = spec.output_dir
        self._sync_record()
        return True

    def get_status(self) -> dict:
        m = self.tracker.metrics
        return {
            "job_id": self.job_id,
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
