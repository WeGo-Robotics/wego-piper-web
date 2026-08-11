"""학습 — 러너(실행 방식) · 메트릭(파싱) · 명세로 나뉜다.

`from app.services.training import train_manager` 로 쓴다.
"""

from app.services.training.manager import TrainManager, train_manager
from app.services.training.metrics import MetricsTracker, TrainHistory, TrainMetrics
from app.services.training.spec import TrainJobSpec

__all__ = [
    "MetricsTracker",
    "TrainHistory",
    "TrainJobSpec",
    "TrainManager",
    "TrainMetrics",
    "train_manager",
]
