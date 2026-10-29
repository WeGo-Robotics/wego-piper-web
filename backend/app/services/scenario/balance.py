"""공통 균형기 — 적게 쓴 쪽부터, 동률은 시드 난수 (feature/scenario-designer.md §7).

템플릿 생성기는 라운드의 **모양**을 만들고, 무엇을 어디에 둘지는 여기서 고른다.

세는 것은 두 겹이다: **블록 안 횟수가 먼저, 전체 횟수가 다음.** 블록 안에서 먼저 고르게 맞추면
중간에 멈춘 데이터셋도 고르고(§7-4), 전체 횟수가 동률을 깨므로 블록이 쌓여도 고르다.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Hashable, Iterable, Sequence
from typing import TypeVar

T = TypeVar("T")


class Tally:
    """키별 횟수 — 블록 안과 전체를 같이 센다."""

    def __init__(self) -> None:
        self.total: Counter = Counter()
        self.block: Counter = Counter()

    def new_block(self) -> None:
        self.block = Counter()

    def add(self, key: Hashable, n: int = 1) -> None:
        self.total[key] += n
        self.block[key] += n

    def score(self, key: Hashable) -> tuple[int, int]:
        return (self.block[key], self.total[key])


def pick_least(
    candidates: Sequence[T],
    score: Callable[[T], tuple],
    rng: random.Random,
) -> T:
    """점수가 가장 작은 후보 하나. 동률은 난수로 — 순서에 기대면 칸이 한쪽부터 채워진다."""
    if not candidates:
        raise ValueError("후보가 없다")
    scored = [(score(c), c) for c in candidates]
    best = min(s for s, _ in scored)
    return rng.choice([c for s, c in scored if s == best])


def apportion(total: int, weights: dict[str, float]) -> dict[str, int]:
    """비율을 정수 개수로 — 최대 나머지 방식. 합이 `total` 을 넘지 않는다.

    비율 합이 1 보다 작으면 남는 몫은 어느 키에도 안 간다(정상 시도의 몫이다).
    """
    raw = {k: max(0.0, w) * total for k, w in weights.items()}
    out = {k: int(v) for k, v in raw.items()}
    want = min(total, round(sum(raw.values())))
    rest = sorted(raw, key=lambda k: (-(raw[k] - out[k]), k))
    for k in rest:
        if sum(out.values()) >= want:
            break
        if raw[k] - out[k] > 0:
            out[k] += 1
    return out


def spread(values: Iterable[int]) -> int:
    """최대 − 최소. 비었으면 0."""
    vs = list(values)
    return (max(vs) - min(vs)) if vs else 0
