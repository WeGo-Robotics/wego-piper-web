"""학습 job 명세 — **실행 방식과 무관한 데이터.**

로컬 subprocess 든 원격 SSH 든 같은 것을 받는다.
지금은 `cmd` 를 그대로 담지만, `build_train_args` 의 경로 의존을 걷어낸 뒤
(feature/cloud-training.md 2단계) 원격에서 인자를 조립할 수 있게 된다.
"""

from dataclasses import dataclass, field


@dataclass
class TrainJobSpec:
    cmd: list[str]
    total_steps: int = 0
    output_dir: str = ""
    # AMP 는 CLI 인자가 아니라 환경변수(`ACCELERATE_MIXED_PRECISION`)로 주입된다.
    # 러너 인터페이스가 `cmd` 만 받으면 이게 조용히 빠지므로 1급 필드로 둔다.
    env: dict[str, str] = field(default_factory=dict)
    #: 학습 프로세스 자체의 시간 상한(시간). 0 이면 안 건다.
    #:
    #: ⚠ **임대 GPU 에서만 뜻이 있는 것이 아니다** — 하지만 거기서는 없으면 돈이 샌다.
    #: 사람·게이트웨이·인터넷이 전부 사라져도 학습은 반드시 끝나야 한다(§6-1). 로컬
    #: 학습은 기본 0 이라 지금과 똑같이 돈다.
    max_hours: float = 0.0
