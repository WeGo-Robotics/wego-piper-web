"""piper_phase — 작업 단계(phase) 라벨러.

**백엔드 라벨러와 wrapper 온라인 추정기가 같은 코드를 쓴다** — 두 벌로 나뉘면
학습 데이터와 추론 입력이 어긋난다 (feature/01-phase-annotation.md §3.4).
`bus/` 와 같은 방식의 설치 가능한 패키지다: `pip install -e phase/`.
"""

from piper_phase.fsm import (
    ALIGN, APPROACH, DONE, GRASP, HOLD, IDLE, PHASE_NAMES, RELEASE,
    Params, PhaseFSM, Signals,
    attach_wrist, compute_signals, count_cycles, finalize, label_causal,
    label_episode, segments,
)

__all__ = [
    "ALIGN", "APPROACH", "DONE", "GRASP", "HOLD", "IDLE", "PHASE_NAMES", "RELEASE",
    "Params", "PhaseFSM", "Signals",
    "attach_wrist", "compute_signals", "count_cycles", "finalize", "label_causal",
    "label_episode", "segments",
]
