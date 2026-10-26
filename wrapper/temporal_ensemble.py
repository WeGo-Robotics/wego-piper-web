"""시간 앙상블(temporal ensembling) — 비동기 추론 루프 판 (ACT 논문 §3.3).

LeRobot 의 `ACTTemporalEnsembler` 는 `ACTPolicy.select_action` 안에서만 돈다 — 매 스텝
**동기로** 추론하고 청크를 한 칸씩 밀어 넣는다는 가정이다. wrapper 는 추론을 별도 스레드에서
돌리며 `predict_action_chunk` 를 직접 부르므로 그 경로를 안 탄다. 그래서 예전 슬라이더
(`temporal_ensemble_coeff` 를 `policy.config` 에 쓰기만 했다)는 처음부터 아무 효과가 없었다.
같은 평균을 여기서 낸다.

## 무엇을 평균하나

청크 하나는 "관측을 찍은 스텝 s 부터의 액션" 이다 — `actions[k]` 는 절대 스텝 `s + k` 의 예측.
스텝 t 에 보낼 액션 = t 를 예측한 모든 청크의 가중 평균:

    a(t) = Σ exp(m·k)·a_k / Σ exp(m·k)        (k = t − s, 그 청크 안에서의 자리)

ACT 의 정의는 wᵢ = exp(−m·i), i 는 **오래된 예측부터** 센 번호다. 스텝 t 의 예측이 K+1 개면
가장 오래된 것이 k=K, 가장 새것이 k=0 이라 wᵢ = exp(−m·(K−k)) = exp(−m·K)·exp(m·k) —
정규화하면 앞의 상수가 지워져 **k 만의 함수**가 된다. 그래서 도착 순서·지연과 상관없이 칸마다
따로 누적할 수 있다. m>0 이면 오래된(멀리 내다본) 예측을 더 믿는다 — 원 논문 기본 0.01, 0 은
균등 평균. 동기·지연 0 이면 LeRobot 과 수치가 같다 (backend/tests/test_temporal_ensemble.py).

평균은 역정규화한 액션 위에서 한다. 역정규화는 차원마다 아핀이고 가중치 합이 1 이라
정규화 공간에서 평균한 것과 같다.

## 비동기라서 다른 점

- 지연: 스텝 s 의 관측으로 낸 청크는 s 가 지나서 도착한다. 이미 보낸 칸은 버린다 — 동기판에서
  가장 새 예측(k=0)이 빠지는 것과 같다.
- 추론이 제어보다 느리면 청크가 몇 스텝에 하나씩 온다. 평균에 드는 예측이 그만큼 적을 뿐이다.
- 아무 청크도 덮지 않는 칸이면 `None` — 부르는 쪽이 직전 액션을 유지한다 (큐가 빈 것과 같다).
- `clear()` 는 세대(`epoch`)를 올린다. 관측을 찍을 때의 세대를 같이 넘기면, 비운 뒤에 도착한
  옛 관측의 청크(일시정지 전·모드 전환 전)가 새 평균에 섞이지 않는다.

torch 를 import 하지 않는다 — 테스트가 그냥 돈다.
"""

from __future__ import annotations

import math

import numpy as np

# exp 가 넘치지 않게. m·k 가 이만큼이면 이미 가장 오래된 예측 하나가 전부다.
_MAX_EXPONENT = 700.0


class TemporalEnsembler:
    """절대 스텝별 가중합. 스레드 안전하지 않다 — 부르는 쪽이 락을 잡는다."""

    def __init__(self, coeff: float = 0.01) -> None:
        self.coeff = float(coeff)
        self.epoch = 0
        self._num: dict[int, np.ndarray] = {}
        self._den: dict[int, float] = {}
        self._next = 0            # 다음에 꺼낼 스텝. 그보다 이른 칸은 이미 보냈다

    def clear(self, next_step: int = 0) -> None:
        """다 비우고 `next_step` 부터 다시. 세대가 바뀐다."""
        self._num.clear()
        self._den.clear()
        self._next = next_step
        self.epoch += 1

    def add(self, start_step: int, actions, epoch: int | None = None) -> int:
        """스텝 `start_step` 의 관측으로 낸 청크를 넣는다. 반영한 칸 수를 돌려준다.

        `epoch` 가 지금 세대가 아니면(그 사이에 비웠으면) 통째로 버린다.
        """
        if epoch is not None and epoch != self.epoch:
            return 0
        used = 0
        for k, a in enumerate(actions):
            t = start_step + k
            if t < self._next:
                continue
            w = math.exp(max(-_MAX_EXPONENT, min(self.coeff * k, _MAX_EXPONENT)))
            wa = w * np.asarray(a, dtype=np.float64)
            if t in self._num:
                self._num[t] += wa
                self._den[t] += w
            else:
                self._num[t] = wa
                self._den[t] = w
            used += 1
        return used

    def pop(self, step: int) -> np.ndarray | None:
        """스텝 `step` 에 보낼 액션. `step` 이하 칸은 비운다 — 늦게 온 청크가 지난 칸을 못 채운다."""
        for t in [t for t in self._num if t < step]:
            del self._num[t]
            del self._den[t]
        self._next = step + 1
        num = self._num.pop(step, None)
        den = self._den.pop(step, None)
        return None if num is None else num / den

    def __len__(self) -> int:
        """앞으로 보낼 칸 중 예측이 있는 칸 수 (텔레메트리·CSV 의 큐 크기 자리)."""
        return len(self._num)
