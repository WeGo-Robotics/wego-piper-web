"""systemd 학습 러너 — 유닛 기계장치는 `services/systemd_process.py` 가 갖는다.

여기 남는 것은 **학습 고유의 것**뿐이다: 유닛 이름 규칙과 `TrainJobSpec` 왕복.
정책 서버·업로드도 같은 기계장치를 쓰므로, 공용 부분을 여기 두면 갈라진다.
"""

import logging

from app.services.process_manager import ProcessState
from app.services.systemd_process import SystemdProcess, available
from app.services.training.spec import TrainJobSpec

logger = logging.getLogger(__name__)

__all__ = ["SystemdRunner", "available"]


class SystemdRunner:
    """`TrainRunner` 구현. 인터페이스는 `LocalRunner` 와 같다."""

    def __init__(self, job_id: str = "local") -> None:
        self.proc = SystemdProcess(f"piper-train-{job_id}")

    @property
    def state(self) -> ProcessState:
        return self.proc.state

    @property
    def is_running(self) -> bool:
        return self.proc.is_running

    @property
    def pid(self) -> int | None:
        return self.proc.pid

    def set_log_callback(self, cb) -> None:
        self.proc.set_log_callback(cb)

    def set_state_callback(self, cb) -> None:
        self.proc.set_state_callback(cb)

    async def start(self, spec: TrainJobSpec) -> None:
        await self.proc.start(list(spec.cmd), spec.env or None)

    async def stop(self) -> None:
        await self.proc.stop()

    def restore(self) -> TrainJobSpec | None:
        """게이트웨이 재시작 후 살아있는 유닛에 다시 붙는다."""
        if not self.proc.reattach():
            return None
        # cmd/총 스텝은 journald 를 다시 읽으며 파서가 채운다 — 지어내지 않는다.
        return TrainJobSpec(cmd=[], total_steps=0, output_dir="")
