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
        # ⚠ **총 스텝을 되찾아야 진행률 바가 뜬다.** 없으면 화면이 "4000 / 0" 이 된다
        # (실기에서 그랬다). 스텝 수는 파서가 journald 로 채우지만 **총계는 로그에
        # 안 나온다** — 유닛의 `ExecStart` 가 사실을 갖고 있다.
        argv = self.proc.exec_argv()
        total, out_dir = 0, ""
        for a in argv:
            if a.startswith("--steps="):
                total = int(a.split("=", 1)[1] or 0)
            elif a.startswith("--output_dir="):
                out_dir = a.split("=", 1)[1]
        logger.info("재부착: 총 %d 스텝, 출력 %s", total, out_dir or "?")
        return TrainJobSpec(cmd=argv, total_steps=total, output_dir=out_dir)
