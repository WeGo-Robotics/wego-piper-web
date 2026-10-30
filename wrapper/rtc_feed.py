"""RTC(Real-Time Chunking) 가 이어 받을 '이전 청크의 남은 원본'을 액션 큐와 나란히 든다.

## 왜

RTC 슬라이더(`max_guidance_weight`·`execution_horizon`)는 처음부터 **아무 효과가 없었다.** LeRobot 의
RTC 는 `predict_action_chunk(obs, inference_delay=d, prev_chunk_left_over=…)` 로 불러야 걸리는데 wrapper
는 관측만 넘겼고, 체크포인트의 `rtc_config` 도 비어 있었다(None). 시간 앙상블과 같은 사고다.

청크 이어 붙이기(chunk_merge.py)가 경계를 크로스페이드해도 SmolVLA-S 는 2~5Hz 가 시연의 1.5~2배로
남았다 — flow matching 이 청크마다 다른 잡음을 뽑아 **청크마다 계획이 조금씩 다르다** (2026-10-28).
RTC 는 새 청크를 만들 때 이전 청크의 남은 부분을 앞에 **박아 넣고(inpainting)** 거기서 이어 가게 한다.
실모델 확인(SmolVLA-S, 같은 관측): 고정한 앞 3칸이 이전 청크와 0.127 → 0.009, 3~10칸 0.148 → 0.063.

## 무엇을 드나

- 원본 — 정규화된, 후처리 **전** 청크. RTC 는 모델 공간에서 잇는다. 큐에 든 것은 후처리·이어 붙이기를
  거친 절대 위치라 못 쓴다. 그래서 큐와 **같은 길이·같은 순서**로 원본을 따로 든다.
- 남은 원본 — 관측 스텝 기준. 0 번째가 그 스텝에 **방금 보낸** 칸이다. 새 청크의 0 번째가 관측 스텝의
  액션이므로(시간 앙상블·청크 이어 붙이기와 같은 규약) 칸이 그대로 맞물린다.
- 추론 지연 — 청크가 도착했을 때 이미 지나간 칸 수(= 이어 붙일 때의 `elapsed`). 최근 몇 번의 **최댓값**을
  쓴다(LeRobot 의 LatencyTracker 와 같다). 크게 잡으면 앞을 더 고정할 뿐이지만, 작게 잡으면 고정 안 된
  칸을 실행하게 된다. 첫 청크(워밍업이라 느리다)는 세지 않는다.

torch 를 import 하지 않는다 — 테스트가 그냥 돈다. 스레드 안전하지 않다 — 부르는 쪽이 락을 잡는다.
"""

from __future__ import annotations

from collections import deque

import numpy as np


class RtcFeed:
    def __init__(self, history: int = 5) -> None:
        self._raw: list[np.ndarray] = []
        self._last: np.ndarray | None = None
        self._elapsed: deque[int] = deque(maxlen=history)

    def clear(self) -> None:
        """리셋·모드 전환 — 이어 받을 것이 없다. 지연 기록도 버린다."""
        self._raw = []
        self._last = None
        self._elapsed.clear()

    def replace(self, raw_chunk, queue_len: int) -> None:
        """새 큐가 섰다. 큐는 청크의 **뒤쪽** `queue_len` 칸이다(앞은 정렬로 버렸다)."""
        raw = np.asarray(raw_chunk, dtype=np.float32)
        self._raw = list(raw[max(0, len(raw) - queue_len):])

    def pop(self) -> None:
        """큐에서 한 칸을 꺼내 보냈다. 큐가 비어 직전 액션을 유지하면 부르지 않는다."""
        if self._raw:
            self._last = self._raw.pop(0)

    def left_over(self) -> np.ndarray | None:
        """`prev_chunk_left_over` (T, A). 아직 아무것도 안 보냈으면 None — 첫 청크는 이어 받을 것이 없다."""
        if self._last is None:
            return None
        return np.stack([self._last, *self._raw])

    def note_elapsed(self, n: int) -> None:
        self._elapsed.append(max(1, int(n)))

    def delay(self, fallback: int = 1) -> int:
        """다음 추론의 `inference_delay`."""
        return max(self._elapsed) if self._elapsed else max(1, int(fallback))
