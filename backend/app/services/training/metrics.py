"""학습 메트릭 파싱과 히스토리 — **실행 방식과 무관하다.**

`lerobot-train` 의 로그 한 줄만 있으면 되므로, 로컬 subprocess 든 원격 SSH 든
같은 코드를 쓴다. 이걸 프로세스 관리에서 떼어내는 것이
클라우드 학습 지원의 절반이다 (feature/cloud-training.md §1-(1)).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

# step:200 smpl:12.8K ep:106 epch:1.07 loss:0.342 grdn:2.157 lr:1.0e-03 updt_s:0.456 data_s:0.012
# step:1K smpl:64K ep:154 epch:3.07 ...  (1000스텝 이후 K 접미사)
METRIC_RE = re.compile(
    r"step:([\d.]+[KM]?)\s+smpl:([\d.]+[KM]?)\s+ep:([\d.]+[KM]?)\s+epch:([\d.]+)\s+"
    r"loss:([\d.]+)\s+grdn:([\d.]+)\s+lr:([\d.e+-]+)\s+"
    r"updt_s:([\d.]+)\s+data_s:([\d.]+)"
)


def parse_num(s: str) -> int:
    """'34K' → 34000, '1.2M' → 1200000, '200' → 200"""
    s = s.strip()
    if s.endswith("K"):
        return int(float(s[:-1]) * 1000)
    elif s.endswith("M"):
        return int(float(s[:-1]) * 1000000)
    return int(s)


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


class MetricsTracker:
    """로그 줄을 먹여주면 메트릭을 뽑아 히스토리에 쌓는다.

    러너(로컬/SSH/…)가 로그를 어디서 가져오든 여기로 흘려보내면 된다.
    """

    def __init__(self) -> None:
        self.metrics = TrainMetrics()
        self.history = TrainHistory()
        self._on_update: Callable[[], None] | None = None

    def set_update_callback(self, cb: Callable[[], None]) -> None:
        self._on_update = cb

    def reset(self, total_steps: int = 0) -> None:
        self.metrics = TrainMetrics(total_steps=total_steps)
        self.history.clear()

    def feed(self, line: str) -> bool:
        """로그 한 줄 처리. 메트릭 줄이었으면 True."""
        m = METRIC_RE.search(line)
        if not m:
            return False
        self.metrics.step = parse_num(m.group(1))
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
        if self._on_update:
            self._on_update()
        return True

    def progress(self) -> float:
        if self.metrics.total_steps > 0 and self.metrics.step > 0:
            return round(self.metrics.step / self.metrics.total_steps, 4)
        return 0.0
