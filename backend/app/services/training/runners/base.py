"""학습 러너 인터페이스.

`TrainManager` 가 이 이음매만 보게 하면, 로컬 subprocess / 원격 SSH /
systemd 유닛이 나란히 붙는다.

- `LocalRunner` — 지금 동작 (subprocess)
- `SSHRunner` — 원격 GPU (feature/cloud-training.md 4단계)
- `SystemdRunner` — 데몬 분리 (refactor/daemon-split.md 6단계)

즉 이 Protocol 하나가 cloud-training 과 daemon-split 양쪽이 기다리는 것이다.
"""

from collections.abc import Callable
from typing import Protocol

from app.services.process_manager import ProcessState
from app.services.training.spec import TrainJobSpec


class TrainRunner(Protocol):
    #: 이 러너로 도는 학습이 **이 기계의 GPU** 를 쓰는가.
    #:
    #: 배타 가드가 이걸 본다. 원격 학습은 로컬 GPU 를 안 쓰므로 추론·녹화를 막을
    #: 이유가 없다 — 오히려 그게 원격으로 보내는 **이유다**
    #: (feature/cloud-training.md 첫 표: "학습은 원격, 로컬 GPU 는 수집/추론 전용").
    occupies_local_gpu: bool

    @property
    def state(self) -> ProcessState: ...

    @property
    def is_running(self) -> bool: ...

    @property
    def pid(self) -> int | None: ...

    def set_log_callback(self, cb: Callable[[str], None]) -> None: ...

    def set_state_callback(self, cb: Callable[[ProcessState], None]) -> None: ...

    async def start(self, spec: TrainJobSpec) -> None: ...

    async def stop(self) -> None: ...

    def restore(self) -> TrainJobSpec | None:
        """서버 재시작 후 살아있는 job 을 복원. 없으면 None."""
        ...
